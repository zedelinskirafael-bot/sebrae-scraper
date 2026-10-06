from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright
from supabase import create_client
import os, asyncio, httpx, re

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
    "/graduar-cliente-maquina", "/os-credenciado", "/debug-login",
}


@app.middleware("http")
async def _serializar_sessao_sebrae(request, call_next):
    if request.url.path in ROTAS_COM_NAVEGADOR:
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
        log.append(f"Token capturado: {token}")
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
