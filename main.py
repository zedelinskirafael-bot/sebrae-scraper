from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright
from supabase import create_client
import os, asyncio, httpx, re

app = FastAPI()

SEBRAE_URL = "https://app2.pr.sebrae.com.br"
SEBRAE_API = "https://api.pr.sebrae.com.br/crm-api"
SEBRAE_USER = os.getenv("SEBRAE_USER")
SEBRAE_PASS = os.getenv("SEBRAE_PASS")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")


class ScrapeRequest(BaseModel):
    codigo_cliente: str
    cliente_id: str


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/debug-login")
async def debug_login():
    resultado = await _debug_completo_login()
    return resultado


@app.post("/buscar-cliente")
async def buscar_cliente(req: ScrapeRequest):
    try:
        token, cod_unidade = await get_token()
        if not token:
            raise Exception("Falha no login — token não encontrado")

        # A API usa o token como App_key no header
        headers = {
            "App_key": token,
            "Authorization": token,
            "Content-Type": "application/json"
        }

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{SEBRAE_API}/agente/{req.codigo_cliente}", headers=headers)
            empresa = r.json() if r.status_code == 200 else {}

            r = await client.get(f"{SEBRAE_API}/agente/{req.codigo_cliente}/telefone", headers=headers)
            telefones_empresa = r.json() if r.status_code == 200 else []

            r = await client.get(f"{SEBRAE_API}/agente/{req.codigo_cliente}/email", headers=headers)
            emails_empresa = r.json() if r.status_code == 200 else []

            r = await client.get(f"{SEBRAE_API}/agente/{req.codigo_cliente}/vinculo", headers=headers)
            socios = r.json() if r.status_code == 200 else []

        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

        supabase.table("clientes").update({
            "nome_fantasia": empresa.get("nomeFantasia") or empresa.get("nome"),
            "cnpj": empresa.get("docId"),
        }).eq("id", req.cliente_id).execute()

        for tel in (telefones_empresa if isinstance(telefones_empresa, list) else []):
            numero = tel.get("numero") or tel.get("telefone") or str(tel)
            if numero:
                supabase.table("telefones").insert({
                    "cliente_id": req.cliente_id,
                    "numero": numero,
                }).execute()

        for em in (emails_empresa if isinstance(emails_empresa, list) else []):
            endereco = em.get("email") or em.get("endereco") or str(em)
            if endereco:
                supabase.table("emails").insert({
                    "cliente_id": req.cliente_id,
                    "email": endereco,
                }).execute()

        pessoas_salvas = []
        async with httpx.AsyncClient(timeout=30) as client:
            for socio in (socios if isinstance(socios, list) else []):
                cod_pf = socio.get("codigo")
                if not cod_pf:
                    continue

                r = await client.get(f"{SEBRAE_API}/agente/{cod_pf}", headers=headers)
                pf = r.json() if r.status_code == 200 else {}

                r = await client.get(f"{SEBRAE_API}/agente/{cod_pf}/telefone", headers=headers)
                tels_pf = r.json() if r.status_code == 200 else []

                r = await client.get(f"{SEBRAE_API}/agente/{cod_pf}/email", headers=headers)
                emails_pf = r.json() if r.status_code == 200 else []

                pessoa_resp = supabase.table("pessoas").insert({
                    "cliente_id": req.cliente_id,
                    "nome": pf.get("nome") or pf.get("descricao"),
                    "apelido": pf.get("nomeFantasia"),
                    "vinculo": socio.get("vinculo", {}).get("descricao") if socio.get("vinculo") else None,
                    "codigo_sebrae": str(cod_pf),
                }).execute()

                pessoa_id = pessoa_resp.data[0]["id"] if pessoa_resp.data else None

                for tel in (tels_pf if isinstance(tels_pf, list) else []):
                    numero = tel.get("numero") or tel.get("telefone") or str(tel)
                    if numero and pessoa_id:
                        supabase.table("telefones").insert({
                            "pessoa_id": pessoa_id,
                            "numero": numero,
                        }).execute()

                for em in (emails_pf if isinstance(emails_pf, list) else []):
                    endereco = em.get("email") or em.get("endereco") or str(em)
                    if endereco and pessoa_id:
                        supabase.table("emails").insert({
                            "pessoa_id": pessoa_id,
                            "email": endereco,
                        }).execute()

                pessoas_salvas.append(pf.get("nome") or str(cod_pf))

        return {
            "sucesso": True,
            "empresa": empresa.get("nomeFantasia") or empresa.get("nome"),
            "socios": pessoas_salvas,
            "debug": {
                "empresa_raw": empresa,
                "socios_raw": socios[:2] if socios else []
            }
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def _debug_completo_login():
    """Debug completo — executa login, navega até CRM e extrai token da URL."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context()
        page = await context.new_page()
        log = []

        try:
            # ETAPA 1: Login
            log.append("Etapa 1: login...")
            await page.goto(f"{SEBRAE_URL}/SebraePR/login.do", wait_until="domcontentloaded")
            await asyncio.sleep(2)
            await page.fill("input[name='usuario']", SEBRAE_USER)
            await page.fill("input[name='senha']", SEBRAE_PASS)
            await page.click("input[type='image'][alt='Ok']")
            await asyncio.sleep(3)
            log.append(f"Etapa 1 OK — URL: {page.url}")

            # ETAPA 2: Confirmar unidade
            log.append("Etapa 2: confirmando unidade...")
            try:
                await page.click("input[type='image'][alt='Entrar no Sistema']", timeout=5000)
            except Exception as e2:
                log.append(f"Etapa 2 erro: {e2}")
            await asyncio.sleep(3)
            log.append(f"Etapa 2 OK — URL: {page.url}")

            # ETAPA 3: Clicar SMART — abre popup
            log.append("Etapa 3: clicando SMART...")
            popup_page = None

            async def handle_popup(popup):
                nonlocal popup_page
                popup_page = popup
                log.append(f"Popup detectado: {popup.url}")

            context.on("page", handle_popup)

            try:
                await page.click("img[src*='btn_smart']", timeout=5000)
            except Exception as e3:
                log.append(f"Click SMART falhou: {e3}")

            await asyncio.sleep(5)

            if popup_page:
                try:
                    await popup_page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    await asyncio.sleep(5)
                log.append(f"Popup carregado: {popup_page.url}")

                # ETAPA 4: Navegar para Pessoas → Cadastro/Consulta
                log.append("Etapa 4: clicando em Pessoas → Cadastro/Consulta...")
                try:
                    # Hover no menu Pessoas para abrir submenu
                    await popup_page.hover("text=Pessoas", timeout=5000)
                    await asyncio.sleep(1)
                    # Clicar em Cadastro/Consulta
                    await popup_page.click("text=Cadastro/Consulta", timeout=5000)
                    await asyncio.sleep(3)
                    log.append(f"URL após Cadastro/Consulta: {popup_page.url}")
                except Exception as e4:
                    log.append(f"Erro ao clicar Pessoas/Cadastro: {e4}")
                    # Tenta navegar direto para a URL do CRM
                    try:
                        await popup_page.goto(
                            f"{SEBRAE_URL}/crm/consultarcliente",
                            wait_until="domcontentloaded",
                            timeout=15000
                        )
                        await asyncio.sleep(3)
                        log.append(f"URL após goto direto: {popup_page.url}")
                    except Exception as e4b:
                        log.append(f"Goto direto também falhou: {e4b}")

                # Extrai token da URL
                url_atual = popup_page.url
                log.append(f"URL final para extração: {url_atual}")

                token = _extrair_token_da_url(url_atual)
                cod_unidade = _extrair_parametro(url_atual, "codUnidade")

                if token:
                    log.append(f"TOKEN ENCONTRADO: {token}")
                    log.append(f"codUnidade: {cod_unidade}")

                    # Testa a API com o token
                    log.append("Testando API com o token...")
                    try:
                        async with httpx.AsyncClient(timeout=15) as client:
                            headers = {
                                "App_key": token,
                                "Authorization": token,
                                "Content-Type": "application/json"
                            }
                            r = await client.get(f"{SEBRAE_API}/agente/268934", headers=headers)
                            log.append(f"API /agente/268934 — status: {r.status_code}")
                            log.append(f"API response: {r.text[:300]}")
                    except Exception as eapi:
                        log.append(f"Erro ao testar API: {eapi}")
                else:
                    log.append("Token NÃO encontrado na URL — verificar HTML da página")
                    # Tenta achar o token no HTML
                    try:
                        html = await popup_page.content()
                        tokens_html = re.findall(
                            r'token[="\s:]+([a-f0-9\-]{36})',
                            html, re.IGNORECASE
                        )
                        log.append(f"Tokens no HTML: {tokens_html[:5]}")
                    except Exception:
                        pass

            await browser.close()
            return {"sucesso": True, "log": log}

        except Exception as e:
            try:
                await browser.close()
            except Exception:
                pass
            return {"sucesso": False, "log": log, "erro": str(e)}


def _extrair_token_da_url(url: str) -> str | None:
    """Extrai o parâmetro 'token' de uma URL."""
    match = re.search(r'[?&]token=([a-f0-9\-]{36})', url, re.IGNORECASE)
    return match.group(1) if match else None


def _extrair_parametro(url: str, param: str) -> str | None:
    """Extrai um parâmetro qualquer de uma URL."""
    match = re.search(rf'[?&]{param}=([^&]+)', url, re.IGNORECASE)
    return match.group(1) if match else None


async def get_token():
    """Executa o login completo e retorna (token, cod_unidade)."""
    async with async_playwright() as p:
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
            # Etapas 1, 2 e 3
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

            if popup_page:
                try:
                    await popup_page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    await asyncio.sleep(5)

                # Navega para Pessoas → Cadastro/Consulta
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
                cod_unidade = _extrair_parametro(url_atual, "codUnidade")

                await browser.close()

                if token:
                    return token, cod_unidade

                raise Exception(f"Token não encontrado na URL: {url_atual}")

            await browser.close()
            raise Exception("Popup não detectado após clicar em SMART")

        except Exception as e:
            try:
                await browser.close()
            except Exception:
                pass
            raise e
