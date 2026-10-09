from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright
from supabase import create_client
import os, asyncio, base64, hmac, httpx, re
from typing import Optional

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# A conta do Sebrae tem SESSAO UNICA (Decisao 4 do Apollo): dois logins
# simultaneos derrubam um ao outro. Toda rota que abre navegador passa por
# esta trava, uma de cada vez. Quem chama em serie (worker, motor) nao sente;
# quem chegar junto (vigia de O.S.) espera a vez.
SMART_LOCK = asyncio.Lock()
ROTAS_COM_NAVEGADOR = {
    "/buscar-cliente", "/buscar-pesquisas", "/analise-risco",
    "/graduar-cliente-maquina", "/os-credenciado", "/nf-credenciado", "/debug-login",
    "/nf-os-dossie", "/nf-incluir",
}
# Rotas privadas: token conferido no middleware, ANTES do lock, para que
# anonimo nao consiga segurar a fila da sessao unica do Sebrae.
ROTAS_PROTEGIDAS = {"/nf-os-dossie", "/nf-incluir", "/debug-login"}


@app.middleware("http")
async def _serializar_sessao_sebrae(request, call_next):
    # nginx publica o app sob /scraper/ e tira o prefixo; o ultimo segmento
    # e o nome da rota nos dois casos.
    bruto = request.url.path.strip("/")
    caminho = "/" + bruto.split("/")[-1] if bruto else "/"
    if caminho in ROTAS_PROTEGIDAS and request.method != "OPTIONS":
        try:
            _exigir_token(request)
        except HTTPException as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status_code)
    if caminho in ROTAS_COM_NAVEGADOR:
        async with SMART_LOCK:
            return await call_next(request)
    return await call_next(request)


SEBRAE_URL = "https://app2.pr.sebrae.com.br"
SEBRAE_API = "https://api.pr.sebrae.com.br/crm-api"
BANCO_PERGUNTAS_API = "https://api.pr.sebrae.com.br/banco-perguntas-api"
SEBRAE_USER = os.getenv("SEBRAE_USER")
SEBRAE_PASS = os.getenv("SEBRAE_PASS")
# Contas do portal de credenciados que o vigia de O.S. pode ler. A do Rafael
# e a padrao (SMART, worker, motor); a da Geovana so serve ao vigia.
CONTAS_SEBRAE = {
    "rafael": (SEBRAE_USER, SEBRAE_PASS),
    "geovana": (os.getenv("SEBRAE_USER_GEOVANA"), os.getenv("SEBRAE_PASS_GEOVANA")),
}
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
APP_KEY = os.getenv("APP_KEY")
# Token das rotas privadas de nota fiscal (ver _exigir_token).
NF_TOKEN = os.getenv("NF_TOKEN")


class ScrapeRequest(BaseModel):
    codigo_cliente: str
    cliente_id: str


class GraduarRequest(BaseModel):
    cnpj: str
    cliente_id: str


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/debug-login")
async def debug_login():
    log = []
    try:
        token = await get_token()
        log.append(f"Token capturado: {len(token)} chars, final ...{token[-4:]}")
        headers = {"App_key": APP_KEY, "Authorization": token, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{SEBRAE_API}/agente/268934", headers=headers)
            log.append(f"API status: {r.status_code}")
            log.append(f"Resposta: {r.text[:300]}")
        return {"sucesso": True, "log": log}
    except Exception as e:
        return {"sucesso": False, "log": log, "erro": str(e)}


@app.get("/os-credenciado")
async def os_credenciado(conta: str = "rafael"):
    """Lista as O.S. do usuario no Portal de Empresas Credenciadas
    (Contratacao > Consultar Os). Consumido pelo vigia de O.S. (vigia-os/),
    que compara com o que ja viu e avisa no WhatsApp. ?conta=rafael|geovana."""
    if conta not in CONTAS_SEBRAE:
        raise HTTPException(status_code=400, detail=f"conta desconhecida: {conta}")
    try:
        async with async_playwright() as p:
            browser, context, page = await _login_menu_geral(p, conta)
            try:
                portal = await _abrir_portal_credenciado(context, page)
                linhas = await _ler_consultar_os(portal)
                try:
                    await portal.goto(f"{SEBRAE_URL}/credenciado/Logout.do", timeout=15000)
                except Exception:
                    pass
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass
        return {"sucesso": True, "os": linhas}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"portal credenciado: {e}")


@app.get("/nf-credenciado")
async def nf_credenciado(conta: str = "rafael", ano_anterior: bool = False):
    """Lista as notas fiscais do credenciado no Portal de Empresas Credenciadas
    (Financeiro > Consultar Nota Fiscal). Consumido pelo vigia de notas
    (vigia-os/vigia-nf.js), que compara com o que ja viu e avisa no WhatsApp.
    ?conta=rafael|geovana. &ano_anterior=true le tambem o ano anterior na
    mesma sessao (virada de ano: nota de dezembro paga em janeiro)."""
    if conta not in CONTAS_SEBRAE:
        raise HTTPException(status_code=400, detail=f"conta desconhecida: {conta}")
    try:
        async with async_playwright() as p:
            browser, context, page = await _login_menu_geral(p, conta)
            try:
                portal = await _abrir_portal_credenciado(context, page)
                await _abrir_consultar_nf(portal)
                notas, paginas = await _ler_consultar_nf(portal)
                if ano_anterior:
                    ano = _ano_brt() - 1
                    mais, pag2 = await _ler_consultar_nf(portal, ano=ano)
                    vistos = {n["codigo"] for n in notas if n.get("codigo")}
                    notas += [n for n in mais if not n.get("codigo") or n["codigo"] not in vistos]
                    paginas += pag2
                try:
                    await portal.goto(f"{SEBRAE_URL}/credenciado/Logout.do", timeout=15000)
                except Exception:
                    pass
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass
        return {"sucesso": True, "notas": notas, "paginas": paginas}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"portal credenciado: {e}")


def _ano_brt():
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone(timedelta(hours=-3))).year


_JS_TABELA_NF = """
() => {
  const limpar = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const corpo = limpar(document.body.innerText);
  const mPag = corpo.match(/Exibir p[aá]gina de\\s*(\\d+)/i);
  const totalPaginas = mPag ? parseInt(mPag[1], 10) : 1;
  const tabelas = [...document.querySelectorAll('table')];
  const t = tabelas.filter(tb => /Num\\. NF/i.test(tb.textContent) && tb.querySelector('tbody tr')).pop();
  if (!t) return { linhas: [], totalPaginas, achouTabela: false };
  const linhaCab = [...t.rows].find(r => /Num\\. NF/i.test(r.textContent) && r.cells.length >= 10);
  if (!linhaCab) return { linhas: [], totalPaginas, achouTabela: false };
  const cab = [...linhaCab.cells].map(c => limpar(c.innerText).toLowerCase());
  const idx = (re) => cab.findIndex(h => re.test(h));
  const iStatus = idx(/^status/), iNF = idx(/^num\\. nf/), iEmp = idx(/^empresa/), iOS = idx(/^num\\. os/),
        iVal = idx(/^valor/), iOpt = idx(/^optante/), iRec = idx(/^data de receb/),
        iApr = idx(/^data de aprop/), iPag = idx(/^data de pag/), iUlt = idx(/intera/);
  const linhas = [];
  for (const r of t.rows) {
    if (r === linhaCab || r.cells.length < 10) continue;
    const c = [...r.cells];
    const txt = (i) => (i >= 0 && c[i]) ? limpar(c[i].innerText) : '';
    const nf = txt(iNF);
    if (!/^\\d+$/.test(nf)) continue;
    const lbl = c[iStatus] && c[iStatus].querySelector('label');
    const mCod = ((lbl && lbl.getAttribute('title')) || '').match(/(\\d{3,})/);
    const lblEmp = c[iEmp] && c[iEmp].querySelector('label');
    const credenciado = limpar(((lblEmp && lblEmp.getAttribute('title')) || '').replace(/Credenciado:/i, ''));
    linhas.push({
      codigo: mCod ? mCod[1] : null, status: txt(iStatus), nf, empresa: txt(iEmp), credenciado,
      os: txt(iOS), valor: txt(iVal), optante_simples: !!(c[iOpt] && c[iOpt].querySelector('img')),
      data_recebimento: txt(iRec), data_apropriacao: txt(iApr), data_pagamento: txt(iPag),
      ultima_interacao: txt(iUlt),
    });
  }
  return { linhas, totalPaginas, achouTabela: true };
}
"""


async def _abrir_consultar_nf(portal):
    """Financeiro (hover) > Consultar Nota Fiscal. Abre o formulario de filtros
    (POST GoConsultarNotaFiscal.do); a lista so chega depois de Pesquisar."""
    await portal.hover("text=Financeiro", timeout=10000)
    await asyncio.sleep(1)
    async with portal.expect_response(lambda r: "GoConsultarNotaFiscal.do" in r.url, timeout=30000):
        await portal.click("text=Consultar Nota Fiscal", timeout=10000)
    await portal.wait_for_selector("input[name='ano']", timeout=30000)
    await asyncio.sleep(1)


async def _pesquisar_nf(portal, ano=None):
    if ano:
        await portal.fill("input[name='ano']", str(ano))
    async with portal.expect_response(lambda r: "GoConsultarNotaFiscalPager" in r.url, timeout=30000):
        await portal.click("text=Pesquisar", timeout=10000)
    await asyncio.sleep(2)


async def _ler_consultar_nf(portal, ano=None):
    """Pesquisa (ano corrente por padrao, filtro Status=Todas) e le a tabela.
    Tenta 99 registros por pagina para nao paginar; se o portal ignorar, anda
    pelas paginas com 'Proximo'. Devolve (notas, paginas_lidas)."""
    await _pesquisar_nf(portal, ano)
    try:
        async with portal.expect_response(lambda r: "GoConsultarNotaFiscalPager" in r.url, timeout=10000):
            await portal.fill("#pagerContainer_qtdePorPag", "99")
            await portal.press("#pagerContainer_qtdePorPag", "Enter")
        await asyncio.sleep(2)
    except Exception:
        pass  # fica com a paginacao padrao (10 por pagina); abaixo anda pelas paginas
    lido = await portal.evaluate(_JS_TABELA_NF)
    notas = list(lido["linhas"])
    paginas = 1
    total = int(lido.get("totalPaginas") or 1)
    for _ in range(min(total - 1, 30)):
        try:
            async with portal.expect_response(lambda r: "GoConsultarNotaFiscalPager" in r.url, timeout=20000):
                await portal.click("text=\"Próximo\"", timeout=5000)
            await asyncio.sleep(2)
        except Exception as e:
            raise Exception(f"nao consegui passar para a pagina {paginas + 1} de {total}: {e}")
        pag = await portal.evaluate(_JS_TABELA_NF)
        novos = [n for n in pag["linhas"] if n.get("codigo") not in {x.get("codigo") for x in notas}]
        if not novos:
            break
        notas += novos
        paginas += 1
    if not notas:
        if not lido.get("achouTabela") and total > 1:
            # Portal diz que ha paginas e o leitor nao achou a tabela: layout mudou.
            raise Exception(f"portal mostra {total} paginas de notas mas nenhuma foi lida (layout mudou?)")
        texto = await portal.evaluate("() => document.body.innerText")
        if not re.search(r"Nenhum|nenhum registro|Exibir p[aá]gina", texto) and not lido.get("achouTabela"):
            raise Exception("tela Consultar Nota Fiscal nao carregou a lista")
    return notas, paginas


async def _abrir_portal_credenciado(context, page):
    """No MENU GERAL, o botao 'Portal do Credenciado' (RedirecionaPCR.do) abre
    o portal em ABA NOVA com o token do SAS. Ir direto na URL nao funciona:
    cai na tela de login do SAS."""
    async with context.expect_page(timeout=20000) as nova:
        await page.click("a[href*='RedirecionaPCR']", timeout=10000)
    portal = await nova.value
    try:
        await portal.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        await asyncio.sleep(5)
    if "/credenciado/" not in portal.url:
        raise Exception(f"portal do credenciado nao abriu (url={portal.url})")
    return portal


_JS_TABELA_OS = """
() => {
  const limpar = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const ehOS = (s) => /^\\d{2}[A-Z]{2,4}\\d{4,}$/.test(limpar(s));
  const tabelas = [...document.querySelectorAll('table')];
  const comCabecalho = tabelas.filter(t => /Fluxo atual/i.test(t.textContent));
  const t = comCabecalho.pop();
  const saida = [];
  if (t) {
    const linhaCab = [...t.rows].find(r => /Fluxo atual/i.test(r.textContent) && r.cells.length >= 6);
    if (linhaCab) {
      const cab = [...linhaCab.cells].map(c => limpar(c.innerText).toLowerCase());
      const idx = (p) => cab.findIndex(h => h.startsWith(p));
      for (const r of t.rows) {
        if (r === linhaCab || r.cells.length < 6) continue;
        const c = [...r.cells].map(x => limpar(x.innerText));
        if (!ehOS(c[idx('os')])) continue;
        saida.push({
          data: c[idx('data')], os: c[idx('os')], solicitante: c[idx('solicitante')],
          equipe: c[idx('equipe')], empresa: c[idx('empresa')], objeto: c[idx('obj')],
          fluxo: c[idx('fluxo')], valor: c[idx('valor')],
        });
      }
    }
  }
  if (saida.length) return saida;
  // Plano B: cabecalho e corpo em tabelas separadas -> posicional.
  for (const tb of tabelas) {
    for (const r of tb.rows) {
      if (r.cells.length < 8) continue;
      const c = [...r.cells].map(x => limpar(x.innerText));
      if (!ehOS(c[1])) continue;
      saida.push({ data: c[0], os: c[1], solicitante: c[2], equipe: c[3],
                   empresa: c[4], objeto: c[5], fluxo: c[6], valor: c[7] });
    }
  }
  return saida;
}
"""


async def _ler_consultar_os(portal):
    """Contratacao (hover) > Consultar Os. A lista chega por POST GoConsultarOS.do
    dentro da mesma pagina (URL nao muda), ordenada da mais recente para a mais
    antiga, 10 por pagina -- O.S. nova sempre aparece na primeira pagina."""
    await portal.hover("text=Contratação", timeout=10000)
    await asyncio.sleep(1)
    async with portal.expect_response(lambda r: "GoConsultarOS" in r.url, timeout=30000):
        await portal.click("text=Consultar Os", timeout=10000)
    await portal.wait_for_selector("text=Fluxo atual", timeout=30000)
    await asyncio.sleep(2)
    linhas = await portal.evaluate(_JS_TABELA_OS)
    if not linhas:
        texto = await portal.evaluate("() => document.body.innerText")
        m = re.search(r"(\d+)\s+Resultados", texto)
        if not m:
            raise Exception("tela Consultar Os nao carregou a lista")
        if int(m.group(1)) > 0:
            # O portal diz que ha linhas e o parser nao achou nenhuma:
            # layout mudou. Falhar alto, nunca devolver [] como sucesso.
            raise Exception(f"portal mostra {m.group(1)} resultados mas nenhum foi lido (layout mudou?)")
    return linhas


# ---------------------------------------------------------------------------
# NOTA FISCAL DO CREDENCIADO -- dossie da O.S. para faturar (rota de LEITURA).
# Financeiro > Consultar Nota Fiscal > Incluir Nota Fiscal mostra os
# "Lancamentos disponiveis" (o que ja foi prestado e pode ser cobrado).
# Esta rota NAO marca checkbox, NAO inclui e NAO grava nada: so le e soma.
# Regra de ouro: o portal e de TERCEIRO (Sebrae) e nao tem desfazer por API.
# Na duvida sobre em que tela esta, a rota ABORTA -- nunca clica "mais um".
# ---------------------------------------------------------------------------

# Rotulos que NUNCA podem ser clicados por engano numa linha de O.S.
_RE_ACAO_PERIGOSA = re.compile(
    r"(?i)cancel|exclu|delet|remov|estorn|imprim|salvar|continuar|incluir|aprovar|aceitar"
)
# Query string do portal carrega token de sessao -- nunca devolver cru.
_RE_SEGREDO = re.compile(
    r"(?i)\b(token|jsessionid|sessionid|jwt|auth|senha|password|pwd|apikey)=([^&\"'\s;]+)"
)


def _mascarar(texto):
    """Esconde token/sessao antes de qualquer coisa sair no corpo da resposta."""
    if not texto:
        return texto
    return _RE_SEGREDO.sub(r"\1=***", texto)


def _exigir_token(request):
    """Rotas de nota fiscal sao privadas. O scraper esta publicado em
    pap-api.linski.com.br/scraper/ sem auth, entao estas exigem o header
    x-nf-token. Sem NF_TOKEN configurado a rota fica fechada (503).
    Chamado pelo middleware ANTES do SMART_LOCK, para que anonimo nao
    consiga segurar a fila da sessao unica do Sebrae."""
    if not NF_TOKEN:
        raise HTTPException(status_code=503, detail="NF_TOKEN nao configurado no servidor")
    enviado = request.headers.get("x-nf-token") or ""
    if not hmac.compare_digest(enviado, NF_TOKEN):
        raise HTTPException(status_code=401, detail="token invalido")


def _valor_br_para_centavos(texto):
    """'R$ 3.885,00' -> 388500 (int). Dinheiro so circula em centavos aqui:
    70 x 55,50 em float binario nao fecha. Devolve None se nao casar e
    levanta se vier negativo (nao existe lancamento negativo nessa tela)."""
    if not texto:
        return None
    m = re.search(r"(\d{1,3}(?:\.\d{3})*|\d+),(\d{2})\b", texto)
    if not m:
        return None
    if re.search(r"-\s*R?\$|\(\s*R\$", texto):
        raise Exception(f"valor negativo entre os lancamentos: {texto}")
    return int(m.group(1).replace(".", "")) * 100 + int(m.group(2))


def _centavos_para_br(centavos):
    inteiro, resto = divmod(int(centavos), 100)
    return f"{inteiro:,}".replace(",", ".") + f",{resto:02d}"


def _horas_para_minutos(texto):
    """'0:30' -> 30 ; '35:00' -> 2100. Aplicado SO na celula de horas."""
    if not texto:
        return None
    m = re.fullmatch(r"\s*(\d{1,4}):([0-5]\d)\s*", texto)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _minutos_para_horas(minutos):
    return f"{minutos // 60}:{minutos % 60:02d}"


def _data_br_para_ordenavel(data):
    """'07/10/2026' -> '20261007' para comparar sem datetime."""
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", data or "")
    if not m:
        return None
    return m.group(3) + m.group(2) + m.group(1)


_JS_LANCAMENTOS = """
() => {
  const limpar = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const corpo = limpar(document.body.innerText);
  const temTitulo = /Lan[cç]amentos dispon[ií]veis/i.test(corpo);
  const telaDepois = /Informa[cç][oõ]es da Nota Fiscal/i.test(corpo);
  const semRegistro = /N[aã]o h[aá] lan[cç]amentos|Nenhum registro|Nenhum lan[cç]amento/i.test(corpo);
  const mPag = corpo.match(/Exibir p[aá]gina de\\s*(\\d+)/i);
  const totalPaginas = mPag ? parseInt(mPag[1], 10) : 1;

  // Cabecalho (quando existir) serve so para conferencia/diagnostico.
  let cabecalho = null;
  for (const r of document.querySelectorAll('tr')) {
    if (/Horas\\s*Utilizadas/i.test(r.textContent) && r.cells.length >= 6
        && !r.querySelector("input[type='checkbox']")) {
      cabecalho = [...r.cells].map(c => limpar(c.innerText));
      break;
    }
  }

  const reOS = /^\\d{2}[A-Z]{2,4}\\d{4,}$/;
  const reValor = /^R\\$\\s*[\\d.]*\\d,\\d{2}$/;
  const reHoras = /^\\d{1,3}:[0-5]\\d$/;
  const reData = /^\\d{2}\\/\\d{2}\\/\\d{4}(\\s+\\d{2}\\/\\d{2}\\/\\d{4})?$/;

  const linhas = [];
  const incompletas = [];
  const caixas = [...document.querySelectorAll("input[type='checkbox'][name='lancamentos']")];
  for (const cb of caixas) {
    const tr = cb.closest('tr');
    if (!tr || !tr.cells) { incompletas.push({ motivo: 'checkbox fora de linha', id: cb.id || null }); continue; }
    const cels = [...tr.cells].map(c => limpar(c.innerText));
    const os = cels.find(t => reOS.test(t)) || null;
    const valor = cels.find(t => reValor.test(t)) || null;
    const horas = cels.find(t => reHoras.test(t)) || null;
    const periodo = cels.find(t => reData.test(t)) || null;
    if (!os || !valor || !horas || !periodo) {
      incompletas.push({ motivo: 'celula nao identificada', id: cb.id || null, celulas: cels });
      continue;
    }
    linhas.push({
      os, valor, horas, periodo,
      datas: periodo.match(/\\d{2}\\/\\d{2}\\/\\d{4}/g) || [],
      celulas: cels,
      marcado: !!cb.checked,
      cb_id: cb.id || null, cb_name: cb.name || null, cb_value: (cb.value || '').slice(0, 120),
    });
  }
  return {
    linhas, incompletas, cabecalho, totalPaginas, temTitulo, telaDepois, semRegistro,
    caixas_encontradas: caixas.length,
    achouTabela: linhas.length > 0,
    indicesOk: incompletas.length === 0,
    motivo: incompletas.length ? 'ha linhas com celula nao identificada' : null,
  };
}
"""

_JS_LINHA_OS = """
(osNumero) => {
  const limpar = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const linha = [...document.querySelectorAll('tr')].filter(r => r.textContent.includes(osNumero)).pop();
  if (!linha) return { achou: false };
  const titles = [...linha.querySelectorAll('[title]')].map(el => el.getAttribute('title'));
  const objeto = titles.slice().sort((a, b) => (b || '').length - (a || '').length)[0] || null;
  const limpo = linha.outerHTML.replace(/\\stitle="[^"]*"/g, ' title="(omitido)"');
  const comOnclick = [];
  linha.querySelectorAll('*').forEach(el => {
    const oc = el.getAttribute('onclick');
    if (oc) comOnclick.push({ tag: el.tagName.toLowerCase(), onclick: oc.slice(0, 200), id: el.id || null });
  });
  if (linha.getAttribute('onclick')) comOnclick.push({ tag: 'tr', onclick: linha.getAttribute('onclick').slice(0, 200), id: linha.id || null });
  const hiddens = [...linha.querySelectorAll("input[type='hidden'], input[type='radio'], input[type='checkbox']")]
    .map(el => ({ tipo: el.type, name: el.name || null, id: el.id || null, valor: (el.value || '').slice(0, 60) }));
  return { achou: true, html_limpo: limpo.slice(0, 5000), objeto: (objeto || '').slice(0, 6000),
           com_onclick: comOnclick, campos: hiddens, celulas: [...linha.cells].map(c => limpar(c.innerText).slice(0, 120)) };
}
"""

_JS_DETALHE_OS = """
() => {
  const txt = document.body.innerText || '';
  const lim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const achar = (re) => { const m = txt.match(re); return m ? lim(m[1]) : null; };

  const periodoTotal = achar(/Per[ií]odo Total[\\s\\S]{0,40}?(\\d{2}\\/\\d{2}\\/\\d{4}[\\s\\S]{1,12}?\\d{2}\\/\\d{2}\\/\\d{4})/i);
  const valorTotal = achar(/Valor Total:?[\\s\\S]{0,20}?R?\\$?\\s*([\\d.]*\\d,\\d{2})/i);
  const local = achar(/LOCAL DE REALIZA[^:]{0,40}:\\s*([^\\n]+)/i);
  const unidade = achar(/Unidade de Atendimento[\\s\\S]{0,40}?\\n\\s*([^\\n]+)/i);

  // Quadro "Servicos contratados": identifica a linha pelo formato das celulas
  // (uma com H:MM e uma com valor), sem depender de indice de coluna.
  let servico = null;
  for (const tb of document.querySelectorAll('table')) {
    if (!/Centro de custo/i.test(tb.textContent)) continue;
    for (const r of tb.rows) {
      const cels = [...r.cells].map(c => lim(c.innerText));
      const h = cels.find(t => /^\\d{1,4}:[0-5]\\d$/.test(t));
      const v = cels.find(t => /^R?\\$?\\s*[\\d.]*\\d,\\d{2}$/.test(t));
      if (h && v) { servico = { celulas: cels, horas: h, valor: v }; break; }
    }
    if (servico) break;
  }
  return { periodo_total: periodoTotal, valor_total: valorTotal, local_realizacao: local,
           unidade, servico_contratado: servico, eh_lista: /Fluxo atual/i.test(txt),
           texto: lim(txt).slice(0, 4000) };
}
"""

_JS_DIAGNOSTICO_TELA = """
() => {
  const limpar = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const clicaveis = [];
  document.querySelectorAll("a, input[type='button'], input[type='submit'], input[type='image'], button").forEach(el => {
    const rotulo = limpar(el.innerText || el.value || el.getAttribute('alt') || el.getAttribute('title') || '');
    if (!rotulo && !el.getAttribute('href') && !el.getAttribute('src')) return;
    clicaveis.push({
      tag: el.tagName.toLowerCase(),
      rotulo: rotulo.slice(0, 80),
      href: (el.getAttribute('href') || '').slice(0, 200),
      onclick: (el.getAttribute('onclick') || '').slice(0, 200),
      src: (el.getAttribute('src') || '').slice(0, 120),
      alt: (el.getAttribute('alt') || '').slice(0, 60),
      name: el.getAttribute('name') || null,
      id: el.id || null,
    });
  });
  const campos = [];
  document.querySelectorAll('input, select, textarea').forEach(el => {
    const tipo = (el.getAttribute('type') || '').toLowerCase();
    const sensivel = (tipo === 'hidden' || tipo === 'password');
    campos.push({
      tag: el.tagName.toLowerCase(), tipo: tipo || null,
      name: el.getAttribute('name') || null, id: el.id || null,
      valor: sensivel ? '***' : (el.value || '').slice(0, 40),
    });
  });
  const primeiraLinhaComOS = (() => {
    for (const r of document.querySelectorAll('tr')) {
      if (/\\b\\d{2}[A-Z]{2,4}\\d{4,}\\b/.test(r.textContent)) return r.outerHTML.slice(0, 4000);
    }
    return null;
  })();
  return {
    url: location.href,
    titulo: document.title,
    clicaveis: clicaveis.slice(0, 150),
    campos: campos.slice(0, 150),
    primeira_linha_com_os: primeiraLinhaComOS,
    texto: limpar(document.body.innerText).slice(0, 8000),
  };
}
"""


def _sanitizar_diagnostico(d):
    """Mascara token de sessao em tudo que volta no corpo da resposta."""
    if not isinstance(d, dict):
        return d
    for chave in ("url", "texto", "primeira_linha_com_os"):
        if d.get(chave):
            d[chave] = _mascarar(d[chave])
    for item in (d.get("clicaveis") or []):
        for chave in ("href", "onclick", "src"):
            if item.get(chave):
                item[chave] = _mascarar(item[chave])
    return d


def _travar_dialogos(pagina):
    """Portal legado dispara confirm() em acoes destrutivas. O default do
    Playwright ja dispensa, mas aqui e explicito: NUNCA aceitar."""
    pagina.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))


async def _voltar_home_credenciado(portal):
    """Depois do detalhe da O.S. o menu de topo pode nao existir. Home.do
    direto funciona porque RedirecionaPCR ja foi clicado nesta sessao."""
    await portal.goto(f"{SEBRAE_URL}/credenciado/Home.do?codUnidade=17", timeout=30000)
    await asyncio.sleep(2)
    if "/credenciado/" not in portal.url:
        raise Exception(f"perdi a sessao do portal do credenciado (url={_mascarar(portal.url)})")


async def _achar_linha_os(portal, os_numero, max_paginas=3):
    """Contratacao > Consultar Os e para na pagina onde a O.S. aparece.
    Devolve (locator da linha, html da linha). Nao clica em nada da linha."""
    await portal.hover("text=Contratação", timeout=10000)
    await asyncio.sleep(1)
    async with portal.expect_response(lambda r: re.search(r"consultaros", r.url, re.I), timeout=30000):
        await portal.click("text=Consultar Os", timeout=10000)
    await portal.wait_for_selector("text=Fluxo atual", timeout=30000)
    await asyncio.sleep(2)

    anterior = None
    for _ in range(max_paginas):
        texto = await portal.evaluate("() => document.body.innerText")
        if os_numero in texto:
            linha = portal.locator("tr").filter(has_text=os_numero).last
            html = await linha.evaluate("el => el.outerHTML.slice(0, 4000)")
            return linha, html
        if texto == anterior:
            break
        anterior = texto
        try:
            async with portal.expect_response(lambda r: re.search(r"consultaros", r.url, re.I), timeout=15000):
                await portal.click("text=\"Próximo\"", timeout=5000)
            await asyncio.sleep(2)
        except Exception:
            break
    raise Exception(f"O.S. {os_numero} nao aparece nas primeiras paginas de Consultar Os desta conta")


async def _abrir_detalhe_os(context, portal, os_numero):
    """Clica a LUPA da O.S. e le o detalhe (periodo total, horas, valor e o
    texto grande de onde sai o bairro). O alvo e escolhido por semantica e
    passa por denylist: se o unico candidato cheirar a acao destrutiva,
    aborta em vez de clicar. Trata detalhe que abre em aba nova."""
    linha, html_linha = await _achar_linha_os(portal, os_numero)

    seletor_lupa = (
        "a[href*='Detalh'], a[href*='detalh'], a[onclick*='etalh'], "
        "a[href*='Visualiz'], a[onclick*='isualiz'], "
        "img[src*='lupa'], img[src*='Lupa'], img[alt*='etalh'], img[title*='etalh'], "
        "input[type='image'][alt*='etalh'], input[type='image'][alt*='isualiz']"
    )
    alvo = linha.locator(seletor_lupa).first
    if await alvo.count() == 0:
        raise Exception(
            f"nao achei a lupa na linha da O.S. {os_numero} -- nao vou clicar no chute. "
            f"linha={html_linha[:1200]}"
        )
    rotulos = await alvo.evaluate(
        "el => [el.getAttribute('alt'), el.getAttribute('title'), el.getAttribute('href'),"
        " el.getAttribute('onclick'), el.getAttribute('src')].join(' ')"
    )
    if _RE_ACAO_PERIGOSA.search(rotulos or ""):
        raise Exception(f"o candidato a lupa parece acao destrutiva ({rotulos[:200]}) -- abortando")

    detalhe = portal
    try:
        async with context.expect_page(timeout=5000) as nova:
            await alvo.click(timeout=10000)
        detalhe = await nova.value
        _travar_dialogos(detalhe)
    except Exception:
        pass  # abriu na mesma aba (caminho normal)
    await asyncio.sleep(3)
    try:
        await detalhe.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass

    lido = await detalhe.evaluate(_JS_DETALHE_OS)
    # Validacao pelo que foi REALMENTE extraido da tela inteira: o texto vem
    # truncado e o objeto da contratacao ocupa os primeiros milhares de chars.
    campos = [k for k in ("periodo_total", "servico_contratado", "unidade") if lido.get(k)]
    if len(campos) < 2 or lido.get("eh_lista"):
        raise Exception(
            f"a tela aberta nao parece o detalhe da O.S. {os_numero} "
            f"(campos={campos}, eh_lista={lido.get('eh_lista')}) -- linha={html_linha[:800]}"
        )
    if detalhe is not portal:
        try:
            await detalhe.close()
        except Exception:
            pass
    return lido, html_linha


async def _abrir_incluir_nf(portal):
    """Financeiro > Consultar Nota Fiscal > Incluir Nota Fiscal.
    Clica UMA vez, so em rotulo ancorado em "Nota Fiscal" (nunca em
    "Incluir" solto, que e o botao de ESCRITA da tela seguinte). Se clicou e
    nao caiu na tela certa, ABORTA -- nao tenta outro seletor."""
    texto = await portal.evaluate("() => document.body.innerText || ''")
    if re.search(r"(?i)lan[cç]amentos\s+dispon[ií]veis", texto):
        return "ja_estava"

    candidatos = [
        "a#btnIncluirNF",
        "a[href*='IncluirNotaFiscal'], a[onclick*='IncluirNotaFiscal']",
        "text=/Incluir\\s+Nota\\s+Fiscal/i",
        "input[type='button'][value*='Incluir Nota']",
        "input[type='submit'][value*='Incluir Nota']",
        "input[type='image'][alt*='Incluir Nota']",
    ]
    escolhido = None
    for seletor in candidatos:
        try:
            if await portal.locator(seletor).count() > 0:
                escolhido = seletor
                break
        except Exception:
            continue
    if not escolhido:
        raise Exception("nao achei o botao 'Incluir Nota Fiscal' na tela de Consultar Nota Fiscal")

    clicou = False
    try:
        async with portal.expect_response(lambda r: re.search(r"notafiscal", r.url, re.I), timeout=30000):
            await portal.click(escolhido, timeout=10000)
            clicou = True
    except Exception as e:
        if not clicou:
            raise Exception(f"nao consegui clicar em '{escolhido}': {e}")
        # O clique saiu; so a espera de resposta falhou (handler jQuery sem
        # navegacao). Confere a tela -- JAMAIS clicar de novo.
    await asyncio.sleep(4)

    lido = await portal.evaluate(_JS_LANCAMENTOS)
    if lido.get("telaDepois") and not lido.get("temTitulo"):
        raise Exception(
            "o clique avancou DEMAIS (cai em 'Informacoes da Nota Fiscal'). "
            "Pode ter sido criada nota no portal -- confira manualmente antes de repetir."
        )
    if not lido.get("temTitulo"):
        raise Exception("cliquei em Incluir Nota Fiscal e nao cai na tela 'Lancamentos disponiveis'")
    return escolhido


async def _ler_lancamentos(portal):
    """Le a grade de lancamentos disponiveis inteira. A O.S. do exemplo tem
    70 linhas de 0:30 -- ler so a primeira pagina faturaria valor a MENOS,
    entao aqui tenta 99 por pagina e, se houver paginacao, anda por ela."""
    try:
        if await portal.locator("#pagerContainer_qtdePorPag").count() > 0:
            await portal.fill("#pagerContainer_qtdePorPag", "99")
            await portal.press("#pagerContainer_qtdePorPag", "Enter")
            await asyncio.sleep(3)
    except Exception:
        pass

    lido = await portal.evaluate(_JS_LANCAMENTOS)
    if not lido.get("temTitulo"):
        raise Exception("nao estou na tela 'Lancamentos disponiveis' -- nao vou somar nada")
    if not lido.get("indicesOk") and not lido.get("semRegistro"):
        raise Exception(
            f"grade com linha ilegivel ({lido.get('motivo')}); "
            f"exemplos={lido.get('incompletas')[:2]}; cabecalho={lido.get('cabecalho')}"
        )

    linhas = list(lido.get("linhas") or [])
    total = int(lido.get("totalPaginas") or 1)
    paginas = 1
    for _ in range(min(total - 1, 20)):
        try:
            await portal.click("text=\"Próximo\"", timeout=5000)
            await asyncio.sleep(2)
        except Exception:
            break
        pag = await portal.evaluate(_JS_LANCAMENTOS)
        chaves = {l.get("cb_value") for l in linhas}
        novos = [l for l in (pag.get("linhas") or []) if l.get("cb_value") not in chaves]
        if not novos:
            break
        linhas += novos
        paginas += 1
    if total > 1 and paginas < total:
        raise Exception(f"a grade tem {total} paginas e so consegui ler {paginas} -- o valor sairia a menos")
    return linhas, lido, paginas


def _extrair_resumo_detalhe(lido):
    """Normaliza o que o JS leu do detalhe. O bairro sai de
    "LOCAL DE REALIZACAO DOS ATENDIMENTOS: CURITIBA - Guaira" (o portal nao
    tem campo Bairro): cidade antes do travessao, bairro depois."""
    local = (lido or {}).get("local_realizacao") or ""
    cidade, bairro = None, None
    if local:
        partes = [x.strip() for x in re.split(r"\s+[-\u2013]\s+", local) if x.strip()]
        if len(partes) >= 2:
            cidade, bairro = partes[0], partes[-1]
        else:
            cidade = partes[0] if partes else None
    servico = (lido or {}).get("servico_contratado") or {}
    periodo = (lido or {}).get("periodo_total")
    if periodo:
        periodo = re.sub(r"\s*(?:at[\u00e9\u00a9e]+|a|-)\s*", " até ", periodo, count=1)
        periodo = re.sub(r"\s+", " ", periodo).strip()
    return {
        "periodo_total": periodo,
        "valor_total": (lido or {}).get("valor_total"),
        "horas_contratadas": servico.get("horas"),
        "valor_contratado": servico.get("valor"),
        "local_realizacao": local or None,
        "cidade": cidade,
        "bairro": bairro,
        "unidade": (lido or {}).get("unidade"),
    }


def _mesmo_valor(a, b):
    """Compara dois valores em texto BR ignorando 'R$' e espacos."""
    so_num = lambda x: re.sub(r"[^\d,.]", "", x or "")
    return bool(a) and bool(b) and so_num(a) == so_num(b)


def _resumir_lancamentos(linhas, os_numero):
    """Filtra os lancamentos da O.S. alvo e soma valor (em centavos), horas e
    periodo realizado. Falha alto se alguma celula nao foi lida."""
    alvo = [l for l in linhas if l.get("os") == os_numero]
    centavos, minutos, datas = [], [], []
    for l in alvo:
        centavos.append(_valor_br_para_centavos(l.get("valor")))
        minutos.append(_horas_para_minutos(l.get("horas")))
        datas.extend(l.get("datas") or [])
    ordenaveis = sorted([p for p in ((_data_br_para_ordenavel(x), x) for x in datas) if p[0]])
    total_centavos = sum(c for c in centavos if c is not None)
    total_minutos = sum(m for m in minutos if m is not None)
    return {
        "quantidade": len(alvo),
        "valor_total_centavos": total_centavos,
        "valor_total_br": _centavos_para_br(total_centavos),
        "horas_total": _minutos_para_horas(total_minutos) if any(m is not None for m in minutos) else None,
        "data_min": ordenaveis[0][1] if ordenaveis else None,
        "data_max": ordenaveis[-1][1] if ordenaveis else None,
        "sem_valor_lido": sum(1 for c in centavos if c is None),
        "sem_horas_lidas": sum(1 for m in minutos if m is None),
        "ja_marcados": sum(1 for l in alvo if l.get("marcado")),
        "linhas": alvo,
    }


async def _nf_os_dossie(os_numero, conta, so_mapear, debug):
    diagnostico = {}
    async with async_playwright() as p:
        browser, context, page = await _login_menu_geral(p, conta)
        portal = None
        try:
            portal = await _abrir_portal_credenciado(context, page)
            _travar_dialogos(portal)

            if so_mapear:
                # Reconhecimento: NAO clica na lupa e NAO marca/inclui nada.
                # Vai ate a grade de lancamentos (abrir a tela nao grava) para
                # conferir a leitura das colunas antes de confiar nos numeros.
                await _achar_linha_os(portal, os_numero)
                diagnostico["linha_os"] = await portal.evaluate(_JS_LINHA_OS, os_numero)
                await _abrir_consultar_nf(portal)
                diagnostico["seletor_usado"] = await _abrir_incluir_nf(portal)
                linhas, lido, paginas = await _ler_lancamentos(portal)
                diagnostico["grade"] = {
                    "cabecalho": lido.get("cabecalho"), "total_paginas": lido.get("totalPaginas"),
                    "paginas_lidas": paginas, "linhas_lidas": len(linhas),
                    "caixas_encontradas": lido.get("caixas_encontradas"),
                    "incompletas": lido.get("incompletas", [])[:3],
                    "amostra": linhas[:3], "ultima": linhas[-1] if linhas else None,
                }
                diagnostico["resumo_da_os"] = {
                    k: v for k, v in _resumir_lancamentos(linhas, os_numero).items() if k != "linhas"
                }
                diagnostico["tela_lancamentos"] = _sanitizar_diagnostico(
                    await portal.evaluate(_JS_DIAGNOSTICO_TELA))
                return {"sucesso": True, "modo": "so_mapear", "conta": conta,
                        "os": os_numero, "debug": diagnostico}

            lido_detalhe, html_linha = await _abrir_detalhe_os(context, portal, os_numero)
            await _voltar_home_credenciado(portal)
            await _abrir_consultar_nf(portal)
            await _abrir_incluir_nf(portal)
            linhas, lido, paginas = await _ler_lancamentos(portal)

            resumo = _resumir_lancamentos(linhas, os_numero)
            if resumo["quantidade"] == 0:
                raise Exception(f"nenhum lancamento disponivel da O.S. {os_numero} -- nada a faturar agora")
            if resumo["sem_valor_lido"] or resumo["sem_horas_lidas"]:
                raise Exception(
                    f"{resumo['sem_valor_lido']} lancamentos sem valor e "
                    f"{resumo['sem_horas_lidas']} sem horas -- nao da para faturar com leitura parcial"
                )
            if resumo["ja_marcados"]:
                raise Exception("a grade veio com lancamento JA MARCADO -- nao e a tela virgem, abortando")

            detalhe = _extrair_resumo_detalhe(lido_detalhe)
            resposta = {
                "sucesso": True,
                "conta": conta,
                "os": os_numero,
                "detalhe_os": detalhe,
                "detalhe_os_texto": (lido_detalhe or {}).get("texto"),
                "lancamentos": resumo,
                "paginas_lidas": paginas,
                "outras_os_na_tela": sorted({l.get("os") for l in linhas if l.get("os") != os_numero}),
                "confere_com_a_os": _mesmo_valor(
                    detalhe.get("valor_total") or detalhe.get("valor_contratado"),
                    resumo["valor_total_br"]),
            }
            if debug:
                diagnostico["html_linha_os"] = html_linha
                diagnostico["cabecalho_grade"] = lido.get("cabecalho")
                diagnostico["tela_lancamentos"] = _sanitizar_diagnostico(
                    await portal.evaluate(_JS_DIAGNOSTICO_TELA))
                resposta["debug"] = diagnostico
            return resposta
        finally:
            if portal is not None:
                try:
                    await portal.goto(f"{SEBRAE_URL}/credenciado/Logout.do", timeout=15000)
                except Exception:
                    pass
            try:
                await browser.close()
            except Exception:
                pass


@app.get("/nf-os-dossie")
async def nf_os_dossie(
    os_param: str = Query(..., alias="os"),
    conta: str = "rafael",
    so_mapear: bool = False,
    debug: bool = False,
):
    """Dossie da O.S. para faturar. SO LEITURA -- nao marca nem inclui nada.
    Le o detalhe da O.S. (lupa) e os lancamentos disponiveis dela, soma valor,
    horas e periodo realizado. ?os=26SGC254512&conta=rafael|geovana
    &so_mapear=true nao clica em lupa nem em Incluir: so devolve o mapa das
    telas (usado para descobrir os seletores reais antes de confiar neles)."""
    if conta not in CONTAS_SEBRAE:
        raise HTTPException(status_code=400, detail=f"conta desconhecida: {conta}")
    os_numero = (os_param or "").strip().upper()
    if not re.fullmatch(r"\d{2}[A-Z]{2,4}\d{4,}", os_numero):
        raise HTTPException(status_code=400, detail=f"numero de O.S. invalido: {os_param}")
    try:
        return await asyncio.wait_for(
            _nf_os_dossie(os_numero, conta, so_mapear, debug), timeout=420
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="o portal demorou demais (420s) -- nada foi gravado")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"portal credenciado: {_mascarar(str(e))}")


# ---------------------------------------------------------------------------
# INCLUSAO DA NOTA FISCAL NO PORTAL (rota de ESCRITA).
# Caminho do Rafael: Financeiro > Consultar Nota Fiscal > Incluir Nota Fiscal
# > marcar os lancamentos da O.S. > "+ Incluir" > "Continuar" > popup OK >
# tela "Informacoes da Nota Fiscal" > numero/datas + XML > "Incluir nota".
# Ids reais do portal (mapeados 09/10/2026): #btnIncluirNF (abre a grade),
# #lancamentoN (checkbox, name=lancamentos), #todosDisponiveis (marca tudo),
# #btnIncluirLancamento (+ Incluir), #btnContinuar, #btnVoltar.
# A rota CONFERE valor, quantidade e horas contra o que foi aprovado antes de
# marcar qualquer coisa, e de novo na tela de resumo. Divergiu, aborta.
# ---------------------------------------------------------------------------

# Dialogo do portal: aceitar so o que for aviso. Nada que fale em cancelar,
# excluir ou estornar e aceitado automaticamente.
_RE_DIALOGO_PERIGOSO = re.compile(r"(?i)cancel|exclu|delet|remov|estorn|descart")


class IncluirNFRequest(BaseModel):
    os: str
    conta: str = "rafael"
    ate: str = "resumo"  # "resumo" para ou "fim" conclui a inclusao
    retomar: bool = False  # abre a nota ja "Em cadastramento" em vez de montar outra
    codigo_nota: Optional[str] = None  # codigo interno do rascunho, quando conhecido
    valor_esperado: str  # "3.885,00" -- conferido antes de marcar
    quantidade_esperada: int
    horas_esperadas: str  # "35:00"
    numero_nf: Optional[str] = None
    data_emissao: Optional[str] = None  # dd/mm/aaaa
    data_envio: Optional[str] = None  # dd/mm/aaaa
    xml_base64: Optional[str] = None
    xml_titulo: Optional[str] = None
    substituir_anexo: bool = False  # troca um XML ja anexado em vez de somar outro


_JS_LINHA_NOTA_CADASTRAMENTO = """
(codigo) => {
  const lim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const achadas = [];
  for (const r of document.querySelectorAll('tr')) {
    const t = lim(r.innerText);
    if (!/Em cadastramento/i.test(t)) continue;
    const lbl = r.querySelector('label[title]');
    const cod = ((lbl && lbl.getAttribute('title')) || '').match(/(\\d{3,})/);
    achadas.push({
      codigo: cod ? cod[1] : null,
      texto: t.slice(0, 300),
      html: r.outerHTML.replace(/\\stitle="[^"]*"/g, ' title="(omitido)"').slice(0, 2500),
    });
  }
  let alvo = null, iAlvo = -1;
  if (codigo) { iAlvo = achadas.findIndex(a => a.codigo === String(codigo)); }
  else if (achadas.length === 1) { iAlvo = 0; }
  if (iAlvo >= 0) {
    alvo = achadas[iAlvo];
    // marca a linha no DOM para o clique nao depender de posicao
    let k = 0;
    for (const r of document.querySelectorAll('tr')) {
      if (!/Em cadastramento/i.test((r.innerText || ''))) continue;
      if (k === iAlvo) { r.setAttribute('data-alvo-claude', '1'); break; }
      k++;
    }
  }
  return { achadas, alvo, quantas: achadas.length };
}
"""

_JS_CONFIRMAR_MODAL = """
(padrao) => {
  const re = new RegExp(padrao, 'i');
  const visivel = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const candidatos = [...document.querySelectorAll('div, td, p, span, form, table')]
    .filter(el => visivel(el) && re.test(el.innerText || ''));
  // do mais interno para o mais externo: o menor container que tem a pergunta
  for (const el of candidatos.reverse()) {
    const sim = [...el.querySelectorAll("a, button, input[type='button'], input[type='submit']")]
      .find(b => /^\\s*sim\\s*$/i.test(b.innerText || b.value || ''));
    if (sim) {
      sim.setAttribute('data-confirma-claude', '1');
      return { achou: true, pergunta: (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 200),
               id: sim.id || null, onclick: (sim.getAttribute('onclick') || '').slice(0, 200),
               tag: sim.tagName.toLowerCase(), href: sim.getAttribute('href') || null,
               html_popup: el.outerHTML.slice(0, 2500) };
    }
  }
  return { achou: false };
}
"""

_JS_EXCLUIR_ANEXO = """
(titulo) => {
  for (const r of document.querySelectorAll('tr')) {
    if (!(r.innerText || '').includes(titulo)) continue;
    const ex = [...r.querySelectorAll('a')].find(
      a => /removerFileUpload/i.test(a.getAttribute('onclick') || ''));
    if (ex) {
      ex.setAttribute('data-excluir-claude', '1');
      return { achou: true, onclick: (ex.getAttribute('onclick') || '').slice(0, 120),
               linha: (r.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 150) };
    }
  }
  return { achou: false };
}
"""

_JS_POPUP = """
() => {
  const visivel = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const msg = document.querySelector('#popup_message');
  if (!msg || !visivel(msg)) return { aberto: false };
  const texto = (msg.innerText || '').replace(/\\s+/g, ' ').trim();
  const ok = document.querySelector('#popup_ok');
  if (ok) ok.setAttribute('data-popup-ok-claude', '1');
  return {
    aberto: true, texto: texto.slice(0, 400), tem_ok: !!ok,
    eh_sucesso: /sucesso/i.test(texto),
    eh_erro: /erro|falha|inv[aá]lid|n[aã]o foi poss[ií]vel|obrigat[oó]ri|vencid/i.test(texto)
             && !/sucesso/i.test(texto),
  };
}
"""

_JS_POS_ENVIO = """
() => {
  const lim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const txt = document.body.innerText || '';
  const visivel = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const popups = [...document.querySelectorAll("[id*='popup'], .popup, .modal, #fancybox-wrap")]
    .filter(visivel)
    .map(el => ({ id: el.id || null, classe: el.className || null, texto: lim(el.innerText).slice(0, 300) }))
    .filter(x => x.texto);
  const m = txt.match(/Status\\s*\\n\\s*([^\\n]+)/i);
  return {
    status: m ? lim(m[1]) : null,
    ainda_em_cadastramento: /Em cadastramento/i.test(m ? m[1] : ''),
    popups_visiveis: popups.slice(0, 6),
    tem_erro: /vencid|pend[eê]ncia|n[aã]o foi poss[ií]vel|obrigat[oó]rio|inv[aá]lid/i.test(txt),
    trecho_erro: (txt.match(/[^\\n]{0,120}(vencid|pend[eê]ncia|n[aã]o foi poss[ií]vel|obrigat[oó]ri|inv[aá]lid)[^\\n]{0,120}/i) || [null])[0],
  };
}
"""

_JS_ANEXOS = """
() => {
  const txt = document.body.innerText || '';
  const m = txt.match(/Arquivos?[\\s\\S]{0,600}/i);
  return {
    texto: (m ? m[0] : txt.slice(0, 600)).replace(/\\s+/g, ' ').trim(),
    tem_xml: /\\.xml/i.test(txt),
    mensagens: [...document.querySelectorAll('.msgSucesso, .msgErro, .mensagem, .alert, .erro')]
      .map(e => (e.innerText || '').replace(/\\s+/g, ' ').trim()).filter(Boolean).slice(0, 5),
  };
}
"""

_JS_RESUMO_NOTA = """
() => {
  const txt = document.body.innerText || '';
  const lim = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const achar = (re) => { const m = txt.match(re); return m ? lim(m[1]) : null; };
  return {
    eh_tela_informacoes: /Informa[cç][oõ]es da Nota Fiscal/i.test(txt),
    status: achar(/Status\\s*\\n\\s*([^\\n]+)/i),
    valor_total: achar(/Valor Total:?\\s*R?\\$?\\s*([\\d.]*\\d,\\d{2})/i),
    quantidade: achar(/Quantidade Lan[cç]amentos:?\\s*(\\d+)/i),
    total_horas: achar(/Total horas:?\\s*(\\d{1,4}:[0-5]\\d)/i),
    tem_campo_numero: !!document.querySelector("input[name*='numero'], input[id*='numero']"),
    texto: lim(txt).slice(0, 3000),
  };
}
"""

_JS_IDENTIFICADOS = """
() => {
  const txt = document.body.innerText || '';
  const m = txt.match(/Lan[cç]amentos identificados para essa nota([\\s\\S]{0,400})/i);
  const marcados = document.querySelectorAll("input[type='checkbox'][name='lancamentos']:checked").length;
  return { trecho: m ? m[1].replace(/\\s+/g, ' ').trim() : null, marcados };
}
"""


def _tratar_dialogo(dialogo, registro):
    """Aceita aviso simples; recusa qualquer dialogo que fale em cancelar,
    excluir ou descartar. Registra a mensagem para aparecer na resposta."""
    msg = dialogo.message or ""
    registro.append({"tipo": dialogo.type, "mensagem": msg[:300]})
    if _RE_DIALOGO_PERIGOSO.search(msg):
        return asyncio.ensure_future(dialogo.dismiss())
    return asyncio.ensure_future(dialogo.accept())


async def _marcar_lancamentos(portal, alvo, todos_sao_da_os):
    """Marca os checkboxes dos lancamentos da O.S. Se a grade inteira for da
    mesma O.S., usa #todosDisponiveis (1 clique); senao marca um a um pelo id.
    Confere no fim que o numero de marcados e exatamente o esperado."""
    if todos_sao_da_os:
        await portal.check("#todosDisponiveis", timeout=10000)
        await asyncio.sleep(2)
    else:
        for item in alvo:
            cb_id = item.get("cb_id")
            if not cb_id:
                raise Exception(f"lancamento sem id de checkbox: {item.get('celulas')}")
            await portal.check(f"#{cb_id}", timeout=10000)
        await asyncio.sleep(1)

    conferido = await portal.evaluate(
        "() => [...document.querySelectorAll(\"input[type='checkbox'][name='lancamentos']:checked\")]"
        ".map(c => c.id)"
    )
    esperados = {i.get("cb_id") for i in alvo}
    if set(conferido) != esperados:
        sobrando = sorted(set(conferido) - esperados)
        faltando = sorted(esperados - set(conferido))
        raise Exception(
            f"selecao nao bate: {len(conferido)} marcados, esperados {len(esperados)}. "
            f"sobrando={sobrando[:5]} faltando={faltando[:5]}"
        )
    return len(conferido)


def _conferir_resumo_portal(info, req):
    """Valor, quantidade e horas que o PORTAL montou tem de bater com o
    aprovado. Qualquer diferenca aborta antes de preencher/enviar."""
    if not _mesmo_valor(info.get("valor_total"), req.valor_esperado):
        raise Exception(
            f"o portal mostra valor {info.get('valor_total')} e o aprovado era "
            f"{req.valor_esperado} -- NAO preenchi nada; confira o rascunho no portal")
    if str(info.get("quantidade")) != str(req.quantidade_esperada):
        raise Exception(
            f"o portal mostra {info.get('quantidade')} lancamentos e o aprovado era "
            f"{req.quantidade_esperada} -- NAO preenchi nada")
    if info.get("total_horas") != req.horas_esperadas:
        raise Exception(
            f"o portal mostra {info.get('total_horas')} horas e o aprovado era "
            f"{req.horas_esperadas} -- NAO preenchi nada")


async def _popup(portal, fechar=True):
    """Le o popup do portal (#popup_message). Se estiver aberto e for erro,
    devolve o texto para quem chamou decidir -- e fecha no OK para nao deixar
    overlay bloqueando os cliques seguintes."""
    p = await portal.evaluate(_JS_POPUP)
    if p.get("aberto") and fechar and p.get("tem_ok"):
        try:
            await portal.evaluate(
                "() => { const b = document.querySelector(\"[data-popup-ok-claude='1']\");"
                " if (b) b.click(); }")
            await asyncio.sleep(2)
        except Exception:
            pass
    return p


async def _conferir_status_na_lista(portal, codigo_nota):
    """Prova final: volta em Financeiro > Consultar Nota Fiscal e le o status
    da nota na lista (tem de sair de 'Em cadastramento')."""
    try:
        await _abrir_consultar_nf(portal)
        notas, _ = await _ler_consultar_nf(portal)
    except Exception as e:
        return {"erro": f"nao consegui reler a lista: {e}"}
    if codigo_nota:
        achada = next((n for n in notas if str(n.get("codigo")) == str(codigo_nota)), None)
        if achada:
            return {k: achada.get(k) for k in ("codigo", "nf", "status", "os", "valor",
                                               "data_recebimento", "data_pagamento")}
    return {"aviso": "nota nao localizada na lista pelo codigo", "total_na_lista": len(notas)}


class _AnexoJaExiste(Exception):
    """Controle interno: o XML ja esta anexado, nao subir de novo."""


async def _abrir_nota_em_cadastramento(portal, codigo_nota):
    """Financeiro > Consultar Nota Fiscal > abre a nota que ficou
    'Em cadastramento'. Usado para concluir a nota ja montada (os lancamentos
    dela saem de 'disponiveis', entao nao da para remontar)."""
    await _abrir_consultar_nf(portal)
    await _pesquisar_nf(portal)
    lido = await portal.evaluate(_JS_LINHA_NOTA_CADASTRAMENTO, codigo_nota)
    if not lido.get("alvo"):
        raise Exception(
            f"nao achei UMA nota 'Em cadastramento' para retomar "
            f"(encontradas={lido.get('quantas')}, codigo pedido={codigo_nota}); "
            f"amostra={[a.get('codigo') for a in (lido.get('achadas') or [])][:5]}"
        )
    alvo = lido["alvo"]
    linha = portal.locator("tr[data-alvo-claude='1']")
    if await linha.count() != 1:
        raise Exception(f"nao consegui marcar a linha da nota a retomar ({await linha.count()} marcadas)")
    seletor_lupa = (
        "img[src*='lupa'], img[src*='Lupa'], img[alt*='Ver'], img[alt*='etalh'], "
        "a[href*='Detalh'], a[onclick*='etalh'], input[type='image'][alt*='Ver']"
    )
    alvo_click = linha.locator(seletor_lupa).first
    if await alvo_click.count() == 0:
        raise Exception(f"nao achei como abrir a nota em cadastramento. linha={alvo.get('html')[:1200]}")
    rotulos = await alvo_click.evaluate(
        "el => [el.getAttribute('alt'), el.getAttribute('title'), el.getAttribute('href'),"
        " el.getAttribute('onclick'), el.getAttribute('src')].join(' ')")
    if _RE_ACAO_PERIGOSA.search(rotulos or ""):
        raise Exception(f"o alvo para abrir a nota parece acao destrutiva ({rotulos[:150]})")
    await alvo_click.click(timeout=10000)
    await asyncio.sleep(4)
    try:
        await portal.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    info = await portal.evaluate(_JS_RESUMO_NOTA)
    if not info.get("eh_tela_informacoes"):
        raise Exception("abri a linha e nao cai na tela 'Informacoes da Nota Fiscal'")
    return info, alvo


async def _concluir_nota(portal, req, diagnostico):
    """Preenche numero/datas, anexa o XML e envia ao Sebrae."""
    faltando = [c for c in ("numero_nf", "data_emissao", "data_envio", "xml_base64")
                if not getattr(req, c)]
    if faltando:
        raise Exception(f"faltam dados para concluir: {faltando}")

    await portal.fill("#numero", req.numero_nf, timeout=10000)
    await portal.fill("#dtEmissao", req.data_emissao, timeout=10000)
    await portal.fill("#dtEnvSebrae", req.data_envio, timeout=10000)
    await asyncio.sleep(1)
    conferido = await portal.evaluate(
        "() => ({numero: (document.querySelector('#numero')||{}).value,"
        " emissao: (document.querySelector('#dtEmissao')||{}).value,"
        " envio: (document.querySelector('#dtEnvSebrae')||{}).value})")
    diagnostico["campos_preenchidos"] = conferido
    if (conferido.get("numero") or "").strip() != req.numero_nf.strip():
        raise Exception(f"o campo numero nao aceitou o valor (ficou {conferido.get('numero')!r})")

    titulo = req.xml_titulo or f"NFS {req.numero_nf}"
    ja_anexado = await portal.evaluate(_JS_ANEXOS)
    tem_esse = (ja_anexado.get("tem_xml")
                and titulo.lower() in (ja_anexado.get("texto") or "").lower())
    if tem_esse and req.substituir_anexo:
        alvo = await portal.evaluate(_JS_EXCLUIR_ANEXO, titulo)
        if not alvo.get("achou"):
            raise Exception(f"pedi para substituir o anexo '{titulo}' e nao achei o Excluir dele")
        await portal.evaluate(
            "() => { const b = document.querySelector(\"[data-excluir-claude='1']\");"
            " if (b) b.click(); }")
        await asyncio.sleep(3)
        conf = await portal.evaluate(_JS_CONFIRMAR_MODAL, "excluir|remover")
        if conf.get("achou"):
            await portal.evaluate(
                "() => { const b = document.querySelector(\"[data-confirma-claude='1']\");"
                " if (b) b.click(); }")
            await asyncio.sleep(3)
        await _popup(portal)
        depois = await portal.evaluate(_JS_ANEXOS)
        if titulo.lower() in (depois.get("texto") or "").lower():
            raise Exception(f"o anexo '{titulo}' continua na lista depois do Excluir")
        diagnostico["anexo_substituido"] = alvo.get("onclick")
        tem_esse = False
    if tem_esse:
        diagnostico["anexo"] = f"'{titulo}' ja estava anexado -- nao subi de novo"
        caminho = None
    else:
        caminho = f"/tmp/nfs-{re.sub(r'[^0-9A-Za-z-]', '', req.numero_nf)}.xml"
        conteudo = base64.b64decode(req.xml_base64)
        # O portal usa parser Java: BOM antes do prolog = "Content is not
        # allowed in prolog". A Spedy entrega o XML COM BOM.
        if conteudo.startswith(b"\xef\xbb\xbf"):
            conteudo = conteudo[3:]
            diagnostico["bom_removido"] = True
        with open(caminho, "wb") as arq:
            arq.write(conteudo)
    try:
        if caminho is None:
            raise _AnexoJaExiste()
        await portal.set_input_files("#ArquivoNotaFiscalArquivo", caminho, timeout=15000)
        await portal.fill("#ArquivoNotaFiscalTitulo", titulo, timeout=10000)
        await asyncio.sleep(1)
        await portal.click("a[onclick*='prepararFileUpload']", timeout=10000)
        await asyncio.sleep(5)
        aviso = await _popup(portal)
        diagnostico["popup_apos_upload"] = aviso
        if aviso.get("aberto") and aviso.get("eh_erro"):
            raise Exception(f"o portal recusou o arquivo: {aviso.get('texto')}")
    except _AnexoJaExiste:
        pass
    finally:
        if caminho:
            try:
                os.remove(caminho)
            except Exception:
                pass

    anexos = await portal.evaluate(_JS_ANEXOS)
    diagnostico["apos_anexar"] = anexos
    if not anexos.get("tem_xml"):
        raise Exception(f"o XML nao aparece na lista de arquivos depois do upload: {anexos}")

    await portal.click("#btnEnviarNota", timeout=10000)
    await asyncio.sleep(4)

    # Modal HTML de confirmacao: "Deseja realmente enviar esta Nota Fiscal?"
    modal = await portal.evaluate(_JS_CONFIRMAR_MODAL, "Deseja realmente enviar esta Nota Fiscal")
    diagnostico["modal_confirmacao"] = modal
    if not modal.get("achou"):
        raise Exception(
            "cliquei em Enviar e nao apareceu a confirmacao esperada -- "
            "conferir no portal antes de repetir")
    # 1a via: clique real. Se um overlay interceptar, o proprio Playwright falha.
    try:
        await portal.click("[data-confirma-claude='1']", timeout=8000)
    except Exception as e:
        diagnostico["erro_click_modal"] = _mascarar(str(e))[:300]
    await asyncio.sleep(5)
    aviso = await _popup(portal, fechar=False)
    if aviso.get("aberto") and aviso.get("eh_erro"):
        await _popup(portal)
        raise Exception(f"o portal recusou o envio: {aviso.get('texto')}")
    enviou = bool(aviso.get("aberto") and aviso.get("eh_sucesso"))
    diagnostico["popup_envio"] = aviso
    if enviou:
        # O popup e a confirmacao do servidor. A tela por baixo so atualiza
        # depois do OK -- ler "Em cadastramento" aqui seria falso negativo.
        await _popup(portal)
    estado = await portal.evaluate(_JS_POS_ENVIO)
    diagnostico["apos_confirmar_1"] = estado

    # 2a via: disparar o handler pelo DOM (o portal usa jQuery; o div
    # #popup_total fica por cima e barra o clique de ponteiro).
    if not enviou and estado.get("ainda_em_cadastramento"):
        await portal.evaluate(
            "() => { const b = document.querySelector(\"[data-confirma-claude='1']\");"
            " if (b) { b.click(); return true; } return false; }")
        await asyncio.sleep(7)
        aviso = await _popup(portal, fechar=False)
        if aviso.get("aberto") and aviso.get("eh_erro"):
            await _popup(portal)
            raise Exception(f"o portal recusou o envio: {aviso.get('texto')}")
        enviou = bool(aviso.get("aberto") and aviso.get("eh_sucesso"))
        if enviou:
            await _popup(portal)
        estado = await portal.evaluate(_JS_POS_ENVIO)
        diagnostico["apos_confirmar_2"] = estado

    try:
        await portal.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass

    final = await portal.evaluate(_JS_ANEXOS)
    final["status_na_tela"] = estado.get("status")
    final["confirmado_pelo_portal"] = enviou
    final["popups_visiveis"] = estado.get("popups_visiveis")
    diagnostico["apos_enviar"] = final
    if not enviou and estado.get("ainda_em_cadastramento"):
        raise Exception(
            "cliquei em Sim nas duas vias e o portal nao confirmou o envio. "
            f"popups={estado.get('popups_visiveis')} erro={estado.get('trecho_erro')}")
    return final


async def _nf_incluir(req):
    os_numero = (req.os or "").strip().upper()
    dialogos = []
    diagnostico = {}
    async with async_playwright() as p:
        browser, context, page = await _login_menu_geral(p, req.conta)
        portal = None
        try:
            portal = await _abrir_portal_credenciado(context, page)
            portal.on("dialog", lambda d: _tratar_dialogo(d, dialogos))

            if req.retomar:
                info, alvo = await _abrir_nota_em_cadastramento(portal, req.codigo_nota)
                diagnostico["nota_retomada"] = {"codigo": alvo.get("codigo")}
                _conferir_resumo_portal(info, req)
                resposta = {
                    "sucesso": True, "etapa": req.ate, "conta": req.conta, "os": os_numero,
                    "retomada": True, "codigo_nota": alvo.get("codigo"),
                    "resumo_do_portal": {k: info.get(k) for k in
                                         ("status", "valor_total", "quantidade", "total_horas")},
                    "dialogos": dialogos, "debug": diagnostico,
                }
                if req.ate == "resumo":
                    diagnostico["mapa_tela_informacoes"] = _sanitizar_diagnostico(
                        await portal.evaluate(_JS_DIAGNOSTICO_TELA))
                    return resposta
                final = await _concluir_nota(portal, req, diagnostico)
                resposta["resultado"] = final
                resposta["status_final"] = await _conferir_status_na_lista(portal, req.codigo_nota)
                return resposta

            await _abrir_consultar_nf(portal)
            await _abrir_incluir_nf(portal)
            linhas, lido, paginas = await _ler_lancamentos(portal)

            resumo = _resumir_lancamentos(linhas, os_numero)
            outras = sorted({l.get("os") for l in linhas if l.get("os") != os_numero})

            # CONFERENCIA 1 -- antes de marcar qualquer coisa.
            if resumo["quantidade"] != req.quantidade_esperada:
                raise Exception(
                    f"quantidade mudou: a grade tem {resumo['quantidade']} lancamentos da O.S. "
                    f"e o aprovado foi {req.quantidade_esperada} -- nada foi marcado"
                )
            if not _mesmo_valor(resumo["valor_total_br"], req.valor_esperado):
                raise Exception(
                    f"valor mudou: grade={resumo['valor_total_br']} aprovado={req.valor_esperado}"
                    " -- nada foi marcado"
                )
            if resumo["horas_total"] != req.horas_esperadas:
                raise Exception(
                    f"horas mudaram: grade={resumo['horas_total']} aprovado={req.horas_esperadas}"
                    " -- nada foi marcado"
                )
            if resumo["ja_marcados"]:
                raise Exception("a grade ja veio com lancamento marcado -- abortando")

            marcados = await _marcar_lancamentos(portal, resumo["linhas"], not outras)

            # "+ Incluir" leva os marcados para "Lancamentos identificados".
            await portal.click("#btnIncluirLancamento", timeout=10000)
            await asyncio.sleep(3)
            identificados = await portal.evaluate(_JS_IDENTIFICADOS)
            diagnostico["identificados"] = identificados

            await portal.click("#btnContinuar", timeout=10000)
            await asyncio.sleep(5)
            try:
                await portal.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass

            info = await portal.evaluate(_JS_RESUMO_NOTA)
            diagnostico["tela_informacoes"] = info
            if not info.get("eh_tela_informacoes"):
                raise Exception(
                    f"nao cai na tela 'Informacoes da Nota Fiscal' depois de Continuar "
                    f"(texto={(info.get('texto') or '')[:300]})"
                )

            # CONFERENCIA 2 -- o que o PORTAL diz que vai para a nota.
            _conferir_resumo_portal(info, req)

            resposta = {
                "sucesso": True,
                "etapa": req.ate,
                "conta": req.conta,
                "os": os_numero,
                "marcados": marcados,
                "resumo_do_portal": {k: info.get(k) for k in
                                     ("status", "valor_total", "quantidade", "total_horas")},
                "dialogos": dialogos,
                "debug": diagnostico,
            }
            if req.ate == "resumo":
                diagnostico["mapa_tela_informacoes"] = _sanitizar_diagnostico(
                    await portal.evaluate(_JS_DIAGNOSTICO_TELA))
                resposta["aviso"] = (
                    "parei na tela de Informacoes da Nota Fiscal. O rascunho ficou "
                    "'Em cadastramento' no portal -- o mesmo estado em que o Rafael deixa "
                    "quando sai para emitir a nota."
                )
                return resposta

            final = await _concluir_nota(portal, req, diagnostico)
            resposta["resultado"] = final
            resposta["status_final"] = await _conferir_status_na_lista(portal, req.codigo_nota)
            return resposta
        finally:
            if portal is not None:
                try:
                    await portal.goto(f"{SEBRAE_URL}/credenciado/Logout.do", timeout=15000)
                except Exception:
                    pass
            try:
                await browser.close()
            except Exception:
                pass


@app.post("/nf-incluir")
async def nf_incluir(req: IncluirNFRequest):
    """Inclui a nota fiscal no Portal de Empresas Credenciadas. ESCREVE no
    portal do Sebrae. Confere valor, quantidade e horas contra o aprovado
    antes de marcar e de novo no resumo do portal; divergiu, aborta.
    ate=resumo para na tela de Informacoes (rascunho Em cadastramento);
    ate=fim preenche numero/datas, anexa o XML e conclui."""
    if req.conta not in CONTAS_SEBRAE:
        raise HTTPException(status_code=400, detail=f"conta desconhecida: {req.conta}")
    if not re.fullmatch(r"\d{2}[A-Z]{2,4}\d{4,}", (req.os or "").strip().upper()):
        raise HTTPException(status_code=400, detail=f"numero de O.S. invalido: {req.os}")
    if req.ate not in ("resumo", "fim"):
        raise HTTPException(status_code=400, detail="ate deve ser 'resumo' ou 'fim'")
    try:
        return await asyncio.wait_for(_nf_incluir(req), timeout=600)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail="o portal demorou demais (600s) -- confira no portal se ficou rascunho",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"portal credenciado: {_mascarar(str(e))}")


@app.post("/buscar-cliente")
async def buscar_cliente(req: ScrapeRequest):
    try:
        token = await get_token()
        headers = {"App_key": APP_KEY, "Authorization": token, "Content-Type": "application/json"}

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{SEBRAE_API}/agente/{req.codigo_cliente}", headers=headers)
            empresa = r.json() if r.status_code == 200 else {}

            r = await client.get(f"{SEBRAE_API}/agente/{req.codigo_cliente}/vinculo", headers=headers)
            socios = r.json() if r.status_code == 200 else []

        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

        cliente_resp = supabase.table("clientes").select("organizacao_id, usuario_id").eq("id", req.cliente_id).single().execute()
        cliente_data = cliente_resp.data or {}
        org_id = cliente_data.get("organizacao_id")
        user_id = cliente_data.get("usuario_id")

        endereco = empresa.get("endereco") or {}
        logradouro = endereco.get("logradouro") or {}
        bairro_obj = endereco.get("bairro") or {}
        geo = endereco.get("geoLocalizacao") or {}
        cep_raw = str(endereco.get("cep") or "")

        data_abertura = None
        data_str = empresa.get("dataAberturaNascimento")
        if data_str:
            data_abertura = data_str[:10]

        supabase.table("clientes").update({
            "nome_fantasia": empresa.get("nomeFantasia") or empresa.get("nome"),
            "data_abertura": data_abertura,
            "rua": logradouro.get("descricao"),
            "numero": endereco.get("numero"),
            "complemento": endereco.get("complemento"),
            "bairro": bairro_obj.get("descricao"),
            "cep": cep_raw,
            "latitude": geo.get("latitude"),
            "longitude": geo.get("longitude"),
        }).eq("id", req.cliente_id).execute()

        for tel in (empresa.get("telefones") or []):
            numero = tel.get("telefone") or tel.get("numero")
            if numero:
                supabase.table("telefones").insert({
                    "organizacao_id": org_id,
                    "usuario_id": user_id,
                    "referencia_id": req.cliente_id,
                    "numero": numero,
                    "tipo": "empresa",
                }).execute()

        # Emails da empresa (originais) — inseridos depois, junto do fallback cruzado
        emails_empresa = [
            em.get("email") for em in (empresa.get("emails") or []) if em.get("email")
        ]

        pessoas_salvas = []
        socios_info = []  # [{"pessoa_id": ..., "emails": [...]}]
        async with httpx.AsyncClient(timeout=30) as client:
            for socio in (socios if isinstance(socios, list) else []):
                cod_pf = socio.get("codigo")
                if not cod_pf:
                    continue

                r = await client.get(f"{SEBRAE_API}/agente/{cod_pf}", headers=headers)
                pf = r.json() if r.status_code == 200 else {}

                pessoa_resp = supabase.table("pessoas").insert({
                    "organizacao_id": org_id,
                    "usuario_id": user_id,
                    "cliente_id": req.cliente_id,
                    "nome": pf.get("nome") or pf.get("descricao"),
                    "codigo_socio": str(cod_pf),
                }).execute()

                pessoa_id = pessoa_resp.data[0]["id"] if pessoa_resp.data else None

                for tel in (pf.get("telefones") or []):
                    numero = tel.get("telefone") or tel.get("numero")
                    if numero and pessoa_id:
                        supabase.table("telefones").insert({
                            "organizacao_id": org_id,
                            "usuario_id": user_id,
                            "referencia_id": pessoa_id,
                            "numero": numero,
                            "tipo": "socio",
                        }).execute()

                emails_pf = [
                    em.get("email") for em in (pf.get("emails") or []) if em.get("email")
                ]
                socios_info.append({"pessoa_id": pessoa_id, "emails": emails_pf})
                pessoas_salvas.append(pf.get("nome") or str(cod_pf))

        # Fallback cruzado empresa <-> socio (avalia o estado ORIGINAL):
        # - empresa sem email  -> pega do 1o socio (na ordem) que tiver
        # - socio sem email    -> pega do email original da empresa
        # Nao copia email de um socio para outro socio.
        email_empresa_orig = emails_empresa[0] if emails_empresa else None
        email_socio_disp = next((s["emails"][0] for s in socios_info if s["emails"]), None)
        if not email_empresa_orig and email_socio_disp:
            emails_empresa = [email_socio_disp]

        for endereco_email in emails_empresa:
            supabase.table("emails").insert({
                "organizacao_id": org_id,
                "usuario_id": user_id,
                "referencia_id": req.cliente_id,
                "endereco": endereco_email,
                "tipo": "empresa",
            }).execute()

        for s in socios_info:
            if not s["pessoa_id"]:
                continue
            lista_emails = s["emails"]
            if not lista_emails and email_empresa_orig:
                lista_emails = [email_empresa_orig]
            for endereco_email in lista_emails:
                supabase.table("emails").insert({
                    "organizacao_id": org_id,
                    "usuario_id": user_id,
                    "referencia_id": s["pessoa_id"],
                    "endereco": endereco_email,
                    "tipo": "socio",
                }).execute()

        return {
            "sucesso": True,
            "empresa": empresa.get("nomeFantasia") or empresa.get("nome"),
            "socios": pessoas_salvas,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/buscar-pesquisas")
async def buscar_pesquisas(req: ScrapeRequest):
    try:
        token = await get_token()
        headers = {"App_key": APP_KEY, "Authorization": token, "Content-Type": "application/json"}

        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(
                f"{BANCO_PERGUNTAS_API}/usuario/pj/{req.codigo_cliente}/pesquisas-respondidas-finalizadas",
                headers=headers
            )
            pesquisas = r.json() if r.status_code == 200 and r.text else []
            if not isinstance(pesquisas, list):
                pesquisas = []

            supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
            cliente_resp = supabase.table("clientes").select("organizacao_id").eq("id", req.cliente_id).single().execute()
            org_id = (cliente_resp.data or {}).get("organizacao_id")

            salvas = 0
            for pesq in pesquisas:
                if not pesq.get("finalizada"):
                    continue
                uuid_pesq = pesq.get("uuidPesquisa")
                cod_resposta = pesq.get("codReposta")
                tipo = pesq.get("nomePesquisaTxt") or "Pesquisa"
                data_preenchimento = pesq.get("dataPreenchimento")

                conteudo = {
                    "razao_social": None,
                    "data_coleta": data_preenchimento,
                    "questionarios": []
                }

                for quest in (pesq.get("questionarios") or []):
                    uuid_q = quest.get("uuidQuestionario")
                    titulo_q = quest.get("tituloQuestionarioTxt")

                    rel = await client.get(
                        f"{BANCO_PERGUNTAS_API}/pesquisa/public//{uuid_pesq}/relatorio-preenchimento"
                        f"?uuidQuestionario={uuid_q}&codResposta={cod_resposta}",
                        headers=headers
                    )
                    if rel.status_code != 200:
                        continue
                    rel_data = rel.json()
                    if not conteudo["razao_social"]:
                        conteudo["razao_social"] = rel_data.get("razaoSocial")

                    perguntas_extraidas = []
                    paginas = ((rel_data.get("questionario") or {}).get("paginas")) or []
                    for pag in paginas:
                        for p in pag.get("perguntas") or []:
                            texto_pergunta = (p.get("tituloTexto") or "")[:1000]
                            opcoes = {}
                            for d in p.get("dominios") or []:
                                if d.get("nmeDominioConfig") == "LISTA_OPCOES_INFORMADA_USUARIO":
                                    op = d.get("opcoes") or {}
                                    for opt in op.get("dominios") or []:
                                        cod = opt.get("cod")
                                        valor = opt.get("valorString")
                                        if cod and valor:
                                            opcoes[cod] = valor
                            resposta = p.get("resposta") or {}
                            valores = []
                            for vr in resposta.get("valoresResposta") or []:
                                cod_d = vr.get("codDominio")
                                if cod_d and cod_d in opcoes:
                                    valores.append(opcoes[cod_d])
                                else:
                                    val_str = vr.get("valorString") or vr.get("valorTexto")
                                    if val_str:
                                        valores.append(str(val_str))
                            perguntas_extraidas.append({
                                "texto": texto_pergunta,
                                "respostas": valores
                            })

                    conteudo["questionarios"].append({
                        "titulo": titulo_q,
                        "perguntas": perguntas_extraidas
                    })

                supabase.table("pesquisas_smart_cliente").upsert({
                    "cliente_id": req.cliente_id,
                    "organizacao_id": org_id,
                    "tipo": tipo,
                    "data_preenchimento": data_preenchimento,
                    "conteudo": conteudo
                }, on_conflict="cliente_id,tipo,data_preenchimento").execute()
                salvas += 1

            return {"sucesso": True, "pesquisas_salvas": salvas, "total_encontradas": len(pesquisas)}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


STOP_NOMES = {"DA", "DE", "DO", "DAS", "DOS", "E"}


def _normalizar_nome(nome: str):
    import unicodedata
    s = unicodedata.normalize("NFD", nome or "").encode("ascii", "ignore").decode()
    tokens = re.sub(r"[^A-Za-z ]", " ", s).upper().split()
    return [t for t in tokens if t not in STOP_NOMES]


def _nomes_similares(a: str, b: str) -> bool:
    """Compara nomes tolerando abreviacoes e nomes intermediarios omitidos.
    Ex: 'Gilberto Alberton Benvenutti' ~ 'Gilberto Benvenutti' -> True."""
    ta, tb = _normalizar_nome(a), _normalizar_nome(b)
    if not ta or not tb:
        return False

    def tok_match(x, y):
        if x == y:
            return True
        # inicial abreviada: "J" ~ "JOAO"
        return (len(x) == 1 and y.startswith(x)) or (len(y) == 1 and x.startswith(y))

    curto, longo = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if not tok_match(curto[0], longo[0]):
        return False
    if not tok_match(curto[-1], longo[-1]):
        return False
    return all(any(tok_match(t, u) for u in longo) for t in curto)


def _parse_data_abertura(s: str):
    """Data de abertura vem como '2025-05-01 00:00:00' ou '2025-05-01'."""
    from datetime import datetime
    if not s:
        return None
    try:
        return datetime.strptime(str(s).strip()[:10], "%Y-%m-%d")
    except Exception:
        return None


def _parse_data_interacao(s: str):
    from datetime import datetime
    if not s:
        return None
    try:
        return datetime.strptime(s.strip()[:16], "%d/%m/%Y %H:%M")
    except Exception:
        try:
            return datetime.strptime(s.strip()[:10], "%d/%m/%Y")
        except Exception:
            return None


PADRAO_VISITA_PAP = re.compile(
    r"Intera[cç][aã]o Gerada Automaticamente Pelo Registro de Uma Visita Pap",
    re.IGNORECASE,
)

PADRAO_CHECKBOX = re.compile(
    r'<p-checkbox[^>]*id="(check-[^"]+)"[^>]*label="([^"]*)"(.*?)</p-checkbox>',
    re.DOTALL,
)


def _extrair_qualificadores(html: str):
    """Extrai checkboxes da secao 'Qualificadores' (para antes de 'Produtos Sebrae')."""
    ini = html.find(">Qualificadores<")
    if ini == -1:
        ini = html.find("Qualificadores")
    if ini == -1:
        return None  # secao nao encontrada
    fim = html.find("Produtos Sebrae", ini)
    trecho = html[ini:fim if fim > -1 else ini + 20000]
    marcados = []
    for m in PADRAO_CHECKBOX.finditer(trecho):
        _id, label, corpo = m.group(1), m.group(2), m.group(3)
        if "ui-state-active" in corpo:
            marcados.append(label)
    return marcados


@app.post("/analise-risco")
async def analise_risco(req: ScrapeRequest):
    """Analise de Risco do cliente no Smart. Sequencia com paradas:
    1. sem pessoas -> TAG preta (para)   2. email @sebrae -> TAG preta (para)
    3. porte Medio/Grande -> TAG vermelha (para); sem porte -> amarela (segue)
    4. qualificador marcado -> TAG preta (para)
    5. participante ~ quem cadastrou -> TAG preta (para)
    6. abre email 6m (Emanuel Sandri + titulo Digital) -> amarela (segue)
    7. interacoes 6m -> amarela + lista 12m (segue)
    8. visita PAP no ano corrente -> vermelha."""
    from datetime import datetime, timedelta

    tags = []
    interacoes = []
    detalhes = {}

    def resultado(parou_em=None):
        return {
            "sucesso": True,
            "tags": tags,
            "interacoes": interacoes,
            "detalhes": detalhes,
            "parou_em": parou_em,
        }

    try:
        async with async_playwright() as p:
            browser, page = await _fazer_login_e_abrir_smart(p)
            try:
                # O token ja costuma estar na URL logo apos o login.
                # So navega pelo menu se nao achar de primeira.
                token = _extrair_token_da_url(page.url)
                if not token:
                    try:
                        await _abrir_crm_consulta(page)
                    except Exception:
                        pass
                    token = _extrair_token_da_url(page.url)
                if not token:
                    raise Exception(f"Token nao encontrado na URL: {page.url}")
                headers = {"App_key": APP_KEY, "Authorization": token, "Content-Type": "application/json"}

                async with httpx.AsyncClient(timeout=40) as client:
                    # 0) Idade da empresa (menos de 1 ano para tudo)
                    r = await client.get(f"{SEBRAE_API}/pj/{req.codigo_cliente}", headers=headers)
                    pj = r.json() if r.status_code == 200 else {}
                    abertura = _parse_data_abertura(pj.get("dataAberturaNascimento"))
                    detalhes["data_abertura"] = pj.get("dataAberturaNascimento")
                    if abertura:
                        idade_dias = (datetime.now() - abertura).days
                        detalhes["idade_meses"] = round(idade_dias / 30.4)
                        if idade_dias < 365:
                            tags.append({
                                "id": "menos_1_ano",
                                "label": "Menos de 1 ano",
                                "cor": "vermelha",
                                "detalhe": f"Aberta em {abertura.strftime('%d/%m/%Y')} — empresa com menos de 1 ano",
                            })
                            return resultado("idade")

                    # 1) Pessoas cadastradas
                    r = await client.get(f"{SEBRAE_API}/agente/{req.codigo_cliente}/vinculo", headers=headers)
                    vinculo = r.json() if r.status_code == 200 and r.text else []
                    if not isinstance(vinculo, list):
                        vinculo = []
                    detalhes["num_pessoas"] = len(vinculo)
                    if len(vinculo) == 0:
                        tags.append({"id": "sem_pessoas", "label": "Sem pessoas cadastradas", "cor": "preta"})
                        return resultado("pessoas")

                    # 2) Emails com @sebrae (empresa + cada pessoa)
                    emails = []
                    r = await client.get(f"{SEBRAE_API}/agente/{req.codigo_cliente}", headers=headers)
                    empresa = r.json() if r.status_code == 200 else {}
                    for em in (empresa.get("emails") or []):
                        if em.get("email"):
                            emails.append(em["email"])
                    for socio in vinculo:
                        cod_pf = socio.get("codigo")
                        if not cod_pf:
                            continue
                        rp = await client.get(f"{SEBRAE_API}/agente/{cod_pf}", headers=headers)
                        pf = rp.json() if rp.status_code == 200 else {}
                        for em in (pf.get("emails") or []):
                            if em.get("email"):
                                emails.append(em["email"])
                    detalhes["emails_verificados"] = emails
                    achado = next((e for e in emails if "@sebrae" in e.lower()), None)
                    if achado:
                        tags.append({"id": "email_sebrae", "label": "@sebrae", "cor": "preta", "detalhe": achado})
                        return resultado("email")

                    # 3) Porte (reusa o pj ja buscado no passo 0)
                    porte_desc = ((pj.get("porte") or {}).get("descricao")) or ""
                    detalhes["porte"] = porte_desc or None
                    if porte_desc:
                        p_upper = porte_desc.upper()
                        ok = p_upper.startswith("MICRO") or p_upper.startswith("PEQUENO") \
                            or p_upper.startswith("EMPREENDEDOR INDIVIDUAL")
                        if not ok:
                            tags.append({"id": "porte", "label": "Problema no porte", "cor": "vermelha",
                                         "detalhe": porte_desc})
                            return resultado("porte")
                    else:
                        tags.append({"id": "sem_porte", "label": "Sem porte", "cor": "amarela"})

                    # 4) Qualificadores (pagina de edicao do cadastro)
                    await page.goto(
                        f"{SEBRAE_URL}/crm/cadastrarPessoaJuridica/{req.codigo_cliente}",
                        wait_until="domcontentloaded", timeout=25000,
                    )
                    await asyncio.sleep(6)
                    html_edit = await page.content()
                    marcados = _extrair_qualificadores(html_edit)
                    detalhes["qualificadores_marcados"] = marcados
                    if marcados is None:
                        raise Exception("Secao Qualificadores nao encontrada na pagina de edicao")
                    if marcados:
                        tags.append({"id": "qualificadores", "label": "Tem qualificadores", "cor": "preta",
                                     "detalhe": ", ".join(marcados)})
                        return resultado("qualificadores")

                    # 5-8) Interacoes do historico de relacionamento
                    r = await client.put(
                        f"{SEBRAE_API}/historico/relacionamentoSmart/{req.codigo_cliente}",
                        headers=headers, content="",
                    )
                    hist = r.json() if r.status_code == 200 and r.text else {}
                    lista = hist.get("listaHistoricoInteracao") or []
                    detalhes["total_interacoes"] = lista[0].get("total") if lista else 0
                    detalhes["interacoes_recebidas"] = len(lista)

                    agora = datetime.now()
                    corte_6m = agora - timedelta(days=183)

                    # 5) Mesmo participante ~ mesmo cadastrante
                    for it in lista:
                        participantes = (it.get("nomeParticipantes") or "")
                        cadastrou = (it.get("quemCadastrou") or "")
                        for parte in re.split(r"[,;/]", participantes):
                            if parte.strip() and _nomes_similares(parte, cadastrou):
                                tags.append({
                                    "id": "mesmo_participante",
                                    "label": "Mesmo participante, mesmo cadastrante",
                                    "cor": "preta",
                                    "detalhe": f"{parte.strip()} ~ {cadastrou} em {it.get('dataInclusao')}",
                                })
                                return resultado("mesmo_participante")

                    abre_email = False
                    tem_interacao_6m = False
                    visita_pap_ano = None

                    for it in lista:
                        dt = _parse_data_interacao(it.get("dataInclusao"))
                        titulo = it.get("titulo") or ""
                        descricao = it.get("descricao") or ""
                        cadastrou = (it.get("quemCadastrou") or "").strip().upper()

                        # Lista COMPLETA de interacoes (detalhe cheio, sem corte de janela)
                        interacoes.append({
                            "feita_em": it.get("dataInclusao"),
                            "protocolo": it.get("protocolo"),
                            "participante": it.get("nomeParticipantes"),
                            "titulo": titulo or None,
                            "descricao": descricao or None,
                            "quem_cadastrou": it.get("quemCadastrou"),
                        })

                        # 6) Abre email (6 meses, Emanuel Sandri + titulo Digital)
                        if dt and dt >= corte_6m and cadastrou == "EMANUEL SANDRI" \
                                and titulo.upper().startswith("DIGITAL"):
                            abre_email = True

                        # 7) Qualquer interacao nos ultimos 6 meses
                        if dt and dt >= corte_6m:
                            tem_interacao_6m = True

                        # 8) Visita PAP no ano corrente
                        if PADRAO_VISITA_PAP.search(descricao) or PADRAO_VISITA_PAP.search(titulo):
                            if dt and dt.year == agora.year and visita_pap_ano is None:
                                visita_pap_ano = it.get("dataInclusao")

                    if abre_email:
                        tags.append({"id": "abre_email", "label": "Abre email", "cor": "amarela"})
                    if tem_interacao_6m:
                        tags.append({"id": "interacoes", "label": "Interações", "cor": "amarela"})
                    if visita_pap_ano:
                        tags.append({"id": "ja_teve_pap", "label": "Já teve porta a porta", "cor": "vermelha",
                                     "detalhe": visita_pap_ano})

                    return resultado(None)
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/graduar-cliente-maquina")
async def graduar_cliente_maquina(req: GraduarRequest):
    cnpj = re.sub(r"\D", "", req.cnpj or "")
    if len(cnpj) != 14:
        raise HTTPException(status_code=400, detail=f"CNPJ invalido: {req.cnpj}")

    try:
        async with async_playwright() as p:
            browser, popup_page = await _fazer_login_e_abrir_smart(p)
            try:
                codigo = await _buscar_codigo_por_cnpj(popup_page, cnpj)
                if not codigo:
                    return {"sucesso": True, "encontrado": False}

                token = _extrair_token_da_url(popup_page.url)
                visitas = await _contar_visitas_pap(popup_page, codigo)
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

        endereco = await _buscar_endereco_smart(codigo, token) if token else None
        pap_data = await _teve_pap_ano_corrente(codigo, token) if token else None

        update_payload = {
            "codigo": codigo,
            "visitas_anteriores": visitas,
            "origem": "sebrae",
        }
        if endereco:
            update_payload.update({
                "cep": endereco.get("cep"),
                "rua": endereco.get("rua"),
                "numero": endereco.get("numero"),
                "complemento": endereco.get("complemento"),
                "bairro": endereco.get("bairro"),
                "latitude": endereco.get("latitude"),
                "longitude": endereco.get("longitude"),
            })

        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        supabase.table("clientes").update(update_payload).eq("id", req.cliente_id).execute()

        return {
            "sucesso": True,
            "encontrado": True,
            "codigo": codigo,
            "visitas": visitas,
            "endereco": endereco,
            "pap_ano_corrente": bool(pap_data),
            "pap_data": pap_data,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def _buscar_endereco_smart(codigo: str, token: str):
    try:
        headers = {"App_key": APP_KEY, "Authorization": token, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{SEBRAE_API}/agente/{codigo}", headers=headers)
            if r.status_code != 200:
                return None
            empresa = r.json() or {}
        endereco = empresa.get("endereco") or {}
        logradouro = endereco.get("logradouro") or {}
        bairro_obj = endereco.get("bairro") or {}
        geo = endereco.get("geoLocalizacao") or {}
        cep_raw = str(endereco.get("cep") or "") or None
        return {
            "cep": cep_raw,
            "rua": logradouro.get("descricao"),
            "numero": endereco.get("numero"),
            "complemento": endereco.get("complemento"),
            "bairro": bairro_obj.get("descricao"),
            "latitude": geo.get("latitude"),
            "longitude": geo.get("longitude"),
        }
    except Exception:
        return None


async def _teve_pap_ano_corrente(codigo: str, token: str):
    """Retorna a data (str) da 1a Visita PAP registrada no ano corrente, ou None.
    Mesma logica do passo 8 de /analise-risco, isolada para o fluxo da Maquina de Vendas."""
    from datetime import datetime
    try:
        headers = {"App_key": APP_KEY, "Authorization": token, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=40) as client:
            r = await client.put(
                f"{SEBRAE_API}/historico/relacionamentoSmart/{codigo}",
                headers=headers, content="",
            )
        hist = r.json() if r.status_code == 200 and r.text else {}
        lista = hist.get("listaHistoricoInteracao") or []
        ano = datetime.now().year
        for it in lista:
            titulo = it.get("titulo") or ""
            descricao = it.get("descricao") or ""
            if PADRAO_VISITA_PAP.search(descricao) or PADRAO_VISITA_PAP.search(titulo):
                dt = _parse_data_interacao(it.get("dataInclusao"))
                if dt and dt.year == ano:
                    return it.get("dataInclusao")
        return None
    except Exception:
        return None


async def get_token() -> str:
    async with async_playwright() as p:
        browser, popup_page = await _fazer_login_e_abrir_smart(p)
        try:
            try:
                await popup_page.hover("text=Pessoas", timeout=5000)
                await asyncio.sleep(1)
                await popup_page.click("text=Cadastro/Consulta", timeout=5000)
                await asyncio.sleep(3)
            except Exception:
                try:
                    await popup_page.goto(
                        f"{SEBRAE_URL}/crm/consultarcliente",
                        wait_until="domcontentloaded",
                        timeout=15000
                    )
                    await asyncio.sleep(3)
                except Exception:
                    pass

            url_atual = popup_page.url
            token = _extrair_token_da_url(url_atual)
            if token:
                return token
            raise Exception(f"Token nao encontrado na URL: {url_atual}")
        finally:
            try:
                await browser.close()
            except Exception:
                pass


async def _login_menu_geral(p, conta: str = "rafael"):
    """Abre o navegador, faz login no SebraePR e entra na unidade.
    Devolve (browser, context, page) parado no MENU GERAL."""
    usuario, senha = CONTAS_SEBRAE.get(conta, (None, None))
    if not usuario or not senha:
        raise Exception(f"conta '{conta}' sem credencial configurada")
    browser = await p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"]
    )
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await page.goto(f"{SEBRAE_URL}/SebraePR/login.do", wait_until="domcontentloaded")
        await asyncio.sleep(2)
        await page.fill("input[name='usuario']", usuario)
        await page.fill("input[name='senha']", senha)
        await page.click("input[type='image'][alt='Ok']")
        await asyncio.sleep(3)

        try:
            await page.click("input[type='image'][alt='Entrar no Sistema']", timeout=5000)
        except Exception:
            pass
        await asyncio.sleep(3)
        return browser, context, page
    except Exception:
        try:
            await browser.close()
        except Exception:
            pass
        raise


async def _fazer_login_e_abrir_smart(p):
    browser, context, page = await _login_menu_geral(p)
    popup_page = None

    async def handle_popup(popup):
        nonlocal popup_page
        popup_page = popup

    context.on("page", handle_popup)

    try:
        try:
            await page.click("img[src*='btn_smart']", timeout=5000)
        except Exception:
            pass
        await asyncio.sleep(5)

        if not popup_page:
            raise Exception("Popup SMART nao detectado")

        try:
            await popup_page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            await asyncio.sleep(5)

        return browser, popup_page

    except Exception:
        try:
            await browser.close()
        except Exception:
            pass
        raise


async def _abrir_crm_consulta(page):
    if "/crm/consultarcliente" in page.url:
        return
    try:
        await page.hover("text=Pessoas", timeout=5000)
        await asyncio.sleep(1)
        await page.click("text=Cadastro/Consulta", timeout=5000)
        await asyncio.sleep(3)
    except Exception:
        pass
    try:
        await page.wait_for_url("**/crm/consultarcliente**", timeout=15000)
    except Exception:
        pass
    if "/crm/consultarcliente" not in page.url:
        raise Exception(
            f"Nao consegui abrir /crm/consultarcliente via menu Pessoas>Cadastro/Consulta. URL atual: {page.url}"
        )
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass


async def _buscar_codigo_por_cnpj(page, cnpj: str):
    await _abrir_crm_consulta(page)

    input_sel = "#input-cnpj input"
    try:
        await page.wait_for_selector(input_sel, state="visible", timeout=25000)
    except Exception:
        html_snip = (await page.content())[:500]
        raise Exception(f"Campo #input-cnpj nao apareceu. URL atual: {page.url} | HTML: {html_snip[:300]}")
    await asyncio.sleep(1)

    try:
        await page.click(input_sel, timeout=3000)
    except Exception:
        pass
    await page.type(input_sel, cnpj, delay=80)
    await asyncio.sleep(1)

    try:
        await page.press(input_sel, "Enter", timeout=2000)
    except Exception:
        pass

    for sel in [
        "button:has-text('Consultar')",
        "button:has-text('Buscar')",
        "button:has-text('Pesquisar')",
        "p-button button",
    ]:
        try:
            await page.click(sel, timeout=1500)
            break
        except Exception:
            continue

    try:
        await page.wait_for_selector("tbody tr td", state="visible", timeout=15000)
    except Exception:
        return None

    try:
        codigo_txt = (await page.locator("tbody tr td:first-child").first.inner_text()).strip()
        if codigo_txt.isdigit() and len(codigo_txt) >= 4:
            return codigo_txt
    except Exception:
        pass

    html = await page.content()
    padroes = [
        r"detalhar\(['\"](\d+)['\"]\)",
        r"detalharAgente\(['\"](\d+)['\"]\)",
        r"codigo=(\d{4,})",
        r"/agente/(\d{4,})",
        r"/pj/(\d{4,})",
    ]
    for pat in padroes:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


async def _contar_visitas_pap(page, codigo: str) -> int:
    padrao = re.compile(
        r"Intera[c\u00e7][a\u00e3]o Gerada Automaticamente Pelo Registro de Uma Visita Pap",
        re.IGNORECASE,
    )
    base_url = f"{SEBRAE_URL}/crm/historicoRelacionamento/pj/{codigo}"

    await page.goto(base_url, wait_until="domcontentloaded", timeout=20000)
    await asyncio.sleep(2)
    html_1 = await page.content()
    total = len(padrao.findall(html_1))

    pags = [int(m) for m in re.findall(r"pagina=(\d+)", html_1)]
    max_pag = max(pags) if pags else 1
    if max_pag > 50:
        max_pag = 50

    for p_num in range(2, max_pag + 1):
        await page.goto(
            f"{base_url}?pagina={p_num}",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        await asyncio.sleep(1)
        html_n = await page.content()
        total += len(padrao.findall(html_n))

    return total


def _extrair_token_da_url(url: str):
    match = re.search(r'[?&]token=([a-f0-9\-]{36})', url, re.IGNORECASE)
    return match.group(1) if match else None
