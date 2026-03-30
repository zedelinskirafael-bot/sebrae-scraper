from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright
from supabase import create_client
import os, asyncio, httpx, urllib.parse, json

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
        app_key, token = await get_token()
        if not app_key or not token:
            raise Exception("Falha no login — token não encontrado")

        headers = {
            "App_key": app_key,
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
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def _debug_completo_login():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context()
        page = await context.new_page()

        log = []
        requisicoes_api = []
        respostas_api = []

        async def on_request(request):
            url = request.url
            if "sebrae.com.br" in url:
                headers = dict(request.headers)
                requisicoes_api.append({
                    "url": url,
                    "method": request.method,
                    "App_key": headers.get("app_key") or headers.get("App_key") or headers.get("APP_KEY"),
                    "Authorization": headers.get("authorization") or headers.get("Authorization"),
                    "outros_headers": {k: v for k, v in headers.items()
                                       if k.lower() not in ["cookie", "user-agent", "accept", "accept-encoding",
                                                             "accept-language", "connection", "referer"]},
                })

        async def on_response(response):
            url = response.url
            if "api.pr.sebrae.com.br" in url:
                try:
                    body = await response.text()
                    respostas_api.append({
                        "url": url,
                        "status": response.status,
                        "body_preview": body[:500],
                    })
                except Exception:
                    pass

        page.on("request", on_request)
        page.on("response", on_response)

        try:
            # ETAPA 1
            log.append("Etapa 1: login...")
            await page.goto(f"{SEBRAE_URL}/SebraePR/login.do", wait_until="domcontentloaded")
            await asyncio.sleep(2)
            await page.fill("input[name='usuario']", SEBRAE_USER)
            await page.fill("input[name='senha']", SEBRAE_PASS)
            await page.click("input[type='image'][alt='Ok']")
            await asyncio.sleep(3)
            log.append(f"Etapa 1 OK — URL: {page.url}")

            # ETAPA 2
            log.append("Etapa 2: confirmando unidade...")
            try:
                await page.click("input[type='image'][alt='Entrar no Sistema']", timeout=5000)
            except Exception as e2:
                log.append(f"Etapa 2 erro: {e2}")
            await asyncio.sleep(3)
            log.append(f"Etapa 2 OK — URL: {page.url}")

            # ETAPA 3 — captura popup
            log.append("Etapa 3: clicando SMART...")
            popup_page = None

            async def handle_popup(popup):
                nonlocal popup_page
                popup_page = popup
                log.append(f"Popup detectado: {popup.url}")
                popup.on("request", on_request)
                popup.on("response", on_response)

            context.on("page", handle_popup)

            try:
                await page.click("img[src*='btn_smart']", timeout=5000)
            except Exception as e3:
                log.append(f"Click img falhou: {e3}")
                try:
                    await page.click("a[href*='smart'], a[href*='SMART']", timeout=3000)
                except Exception as e3b:
                    log.append(f"Click link também falhou: {e3b}")

            await asyncio.sleep(5)

            if popup_page:
                log.append(f"Aguardando popup carregar...")
                try:
                    await popup_page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    await asyncio.sleep(8)
                log.append(f"Popup URL final: {popup_page.url}")

                # Procura token no JavaScript da página
                try:
                    scripts = await popup_page.evaluate("""() => {
                        const results = [];
                        const keys = ['appKey', 'App_key', 'token', 'authToken', 'authorization', 'crmToken', 'APP_KEY'];
                        for (const key of keys) {
                            if (window[key]) results.push({fonte: 'window', key, value: String(window[key]).substring(0, 200)});
                        }
                        try {
                            for (let i = 0; i < localStorage.length; i++) {
                                const k = localStorage.key(i);
                                results.push({fonte: 'localStorage', key: k, value: localStorage.getItem(k).substring(0, 200)});
                            }
                        } catch(e) {}
                        try {
                            for (let i = 0; i < sessionStorage.length; i++) {
                                const k = sessionStorage.key(i);
                                results.push({fonte: 'sessionStorage', key: k, value: sessionStorage.getItem(k).substring(0, 200)});
                            }
                        } catch(e) {}
                        return results;
                    }""")
                    log.append(f"JS globals/storage: {scripts}")
                except Exception as ejs:
                    log.append(f"Erro JS: {ejs}")

                # Captura HTML do popup (primeiros 2000 chars)
                try:
                    html = await popup_page.content()
                    log.append(f"HTML popup (inicio): {html[:2000]}")
                except Exception:
                    pass

            await asyncio.sleep(3)

            cookies_final = await context.cookies()
            cookies_info = [{"name": c["name"], "domain": c["domain"],
                             "value_preview": c["value"][:100]} for c in cookies_final]

            await browser.close()

            return {
                "sucesso": True,
                "log": log,
                "cookies": cookies_info,
                "requisicoes_sebrae": requisicoes_api[:30],
                "respostas_api": respostas_api[:10],
            }

        except Exception as e:
            try:
                await browser.close()
            except Exception:
                pass
            return {"sucesso": False, "log": log, "erro": str(e),
                    "requisicoes_capturadas": requisicoes_api}


async def get_token():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context()
        page = await context.new_page()

        app_key_found = None
        token_found = None

        async def on_request(request):
            nonlocal app_key_found, token_found
            if "api.pr.sebrae.com.br" in request.url:
                headers = dict(request.headers)
                ak = headers.get("app_key") or headers.get("App_key") or headers.get("APP_KEY")
                tk = headers.get("authorization") or headers.get("Authorization")
                if ak:
                    app_key_found = ak
                if tk:
                    token_found = tk

        popup_page = None

        async def handle_popup(popup):
            nonlocal popup_page
            popup_page = popup
            popup.on("request", on_request)

        page.on("request", on_request)
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
                try:
                    await page.click("a[href*='smart'], a[href*='SMART']", timeout=3000)
                except Exception:
                    pass

            await asyncio.sleep(5)

            if popup_page:
                try:
                    await popup_page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    await asyncio.sleep(8)

            await asyncio.sleep(5)

            await browser.close()

            if app_key_found and token_found:
                return app_key_found, token_found

            raise Exception(f"Token não encontrado. app_key={app_key_found}, token={token_found}")

        except Exception as e:
            try:
                await browser.close()
            except Exception:
                pass
            raise e
