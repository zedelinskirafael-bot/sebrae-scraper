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

SEBRAE_URL = "https://app2.pr.sebrae.com.br"
SEBRAE_API = "https://api.pr.sebrae.com.br/crm-api"
BANCO_PERGUNTAS_API = "https://api.pr.sebrae.com.br/banco-perguntas-api"
SEBRAE_USER = os.getenv("SEBRAE_USER")
SEBRAE_PASS = os.getenv("SEBRAE_PASS")
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

        for em in (empresa.get("emails") or []):
            endereco_email = em.get("email")
            if endereco_email:
                supabase.table("emails").insert({
                    "organizacao_id": org_id,
                    "usuario_id": user_id,
                    "referencia_id": req.cliente_id,
                    "endereco": endereco_email,
                    "tipo": "empresa",
                }).execute()

        pessoas_salvas = []
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

                for em in (pf.get("emails") or []):
                    endereco_email = em.get("email")
                    if endereco_email and pessoa_id:
                        supabase.table("emails").insert({
                            "organizacao_id": org_id,
                            "usuario_id": user_id,
                            "referencia_id": pessoa_id,
                            "endereco": endereco_email,
                            "tipo": "socio",
                        }).execute()

                pessoas_salvas.append(pf.get("nome") or str(cod_pf))

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


@app.post("/debug-analise")
async def debug_analise(req: ScrapeRequest):
    """TEMPORARIO — descoberta tecnica para a feature Analise de Risco (v2).
    Grampo de rede: captura as chamadas de API que o Smart faz ao navegar
    pela ficha do cliente e pelo historico. Remover depois."""
    out = {"codigo": req.codigo_cliente}
    cod_nav = req.codigo_cliente.split(",")[0].strip()
    capturas = []

    async def on_response(resp):
        url = resp.url
        if "api.pr.sebrae.com.br" not in url:
            return
        item = {"url": url, "status": resp.status, "metodo": resp.request.method}
        try:
            if resp.request.method in ("PUT", "POST"):
                item["request_body"] = (resp.request.post_data or "")[:800]
        except Exception:
            pass
        try:
            ct = resp.headers.get("content-type", "")
            if "json" in ct and resp.status == 200:
                body = await resp.text()
                limite = 8000 if any(x in url for x in ["/pj/", "relacionamentoSmart", "qualific"]) else 1200
                item["body"] = body[:limite]
        except Exception:
            pass
        capturas.append(item)

    try:
        async with async_playwright() as p:
            browser, page = await _fazer_login_e_abrir_smart(p)
            try:
                page.on("response", on_response)

                # 1) Consulta de cliente — descobrir campos de busca
                await _abrir_crm_consulta(page)
                inputs_html = await page.evaluate(
                    "() => Array.from(document.querySelectorAll('input, p-inputmask, span[id]')).slice(0, 40)"
                    ".map(e => (e.id || '') + '|' + (e.getAttribute('placeholder') || '') + '|' + e.tagName).join('\\n')"
                )
                out["campos_busca"] = inputs_html

                # 2) Buscar por codigo (tentar input de codigo; senao, deixa registrado)
                achou_input = None
                for sel in ["#input-cod input", "#input-cod", "#input-codigo input", "input[placeholder*='digo']"]:
                    try:
                        await page.wait_for_selector(sel, state="visible", timeout=3000)
                        achou_input = sel
                        break
                    except Exception:
                        continue
                out["input_codigo"] = achou_input

                if achou_input:
                    await page.click(achou_input)
                    await page.type(achou_input, cod_nav, delay=60)
                    try:
                        await page.press(achou_input, "Enter", timeout=2000)
                    except Exception:
                        pass
                    for sel in ["button:has-text('Consultar')", "button:has-text('Pesquisar')", "p-button button"]:
                        try:
                            await page.click(sel, timeout=1500)
                            break
                        except Exception:
                            continue
                    await asyncio.sleep(4)
                    # clicar na primeira linha do resultado (ou lupa)
                    for sel in ["tbody tr td a", "tbody tr .ui-row-toggler", "tbody tr td:first-child", "tbody tr"]:
                        try:
                            await page.click(sel, timeout=3000)
                            break
                        except Exception:
                            continue
                    await asyncio.sleep(6)
                    out["url_apos_clique"] = page.url
                    html_detalhe = await page.content()
                    idx = html_detalhe.find("Qualificadores")
                    out["tem_qualificadores_no_html"] = idx > -1
                    if idx > -1:
                        out["trecho_qualificadores"] = html_detalhe[max(0, idx - 200): idx + 3000]

                # 2b) Tentar abrir tela de EDICAO do cadastro (onde moram os Qualificadores)
                if achou_input:
                    for sel in ["button:has-text('Alterar')", "a:has-text('Alterar')",
                                "[title*='Editar']", "[title*='Alterar']",
                                "i.material-icons:has-text('create')", "i.material-icons:has-text('edit')",
                                "img[src*='editar']", "img[src*='alterar']",
                                "tbody tr td a:has(i)", "tbody tr button"]:
                        try:
                            await page.click(sel, timeout=2500)
                            out["clique_editar"] = sel
                            break
                        except Exception:
                            continue
                    await asyncio.sleep(6)
                    out["url_apos_editar"] = page.url
                    html_edit = await page.content()
                    idx = html_edit.find("Qualificadores")
                    out["qualificadores_na_edicao"] = idx > -1
                    if idx > -1:
                        out["trecho_qualificadores"] = html_edit[max(0, idx - 300): idx + 4000]

                # 3) Porte dos codigos extras + probes de qualificadores (via API direta)
                token = _extrair_token_da_url(page.url) or _extrair_token_da_url(out.get("url_apos_clique") or "")
                if token:
                    headers = {"App_key": APP_KEY, "Authorization": token, "Content-Type": "application/json"}
                    codigos = [c.strip() for c in req.codigo_cliente.split(",") if c.strip()]
                    async with httpx.AsyncClient(timeout=30) as client:
                        portes = {}
                        for cod in codigos:
                            try:
                                r = await client.get(f"{SEBRAE_API}/pj/{cod}", headers=headers)
                                j = r.json() if r.status_code == 200 else {}
                                portes[cod] = {
                                    "porte": j.get("porte"),
                                    "indicadorEmpresa": j.get("indicadorEmpresa"),
                                    "nomeFantasia": j.get("nomeFantasia"),
                                }
                            except Exception as ex:
                                portes[cod] = {"erro": str(ex)}
                        out["portes"] = portes

                        probes = {}
                        cod0 = codigos[0]
                        for ep in [f"pj/{cod0}/qualificador", f"pj/{cod0}/qualificadores",
                                   f"qualificador/{cod0}", f"qualificadores/{cod0}",
                                   f"agente/{cod0}/qualificador", f"agente/{cod0}/qualificadores",
                                   f"pj/{cod0}/produto-sebrae", f"pj/{cod0}/produtos-sebrae",
                                   f"agente/{cod0}/marcador", "qualificador", "qualificadores"]:
                            try:
                                rr = await client.get(f"{SEBRAE_API}/{ep}", headers=headers)
                                probes[ep] = {"status": rr.status_code,
                                              "body": rr.text[:500] if rr.status_code == 200 else None}
                            except Exception as ex:
                                probes[ep] = {"erro": str(ex)}
                        out["probes_qualificadores"] = probes
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

        out["capturas"] = capturas
        return out
    except Exception as e:
        out["erro"] = str(e)
        out["capturas"] = capturas
        return out


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


async def _fazer_login_e_abrir_smart(p):
    browser = await p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"]
    )
    context = await browser.new_context()
    page = await context.new_page()
    popup_page = None

    async def handle_popup(popup):
        nonlocal popup_page
        popup_page = popup

    context.on("page", handle_popup)

    try:
        await page.goto(f"{SEBRAE_URL}/SebraePR/login.do", wait_until="domcontentloaded")
        await asyncio.sleep(2)
        await page.fill("input[name='usuario']", SEBRAE_USER)
        await page.fill("input[name='senha']", SEBRAE_PASS)
        await page.click("input[type='image'][alt='Ok']")
        await asyncio.sleep(3)

        try:
            await page.click("input[type='image'][alt='Entrar no Sistema']", timeout=5000)
        except Exception:
            pass
        await asyncio.sleep(3)

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
