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

@app.post("/buscar-cliente", timeout=120)
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
            "debug": {
                "empresa_raw": empresa,
                "socios_raw": socios[:2] if socios else []
            }
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def get_token():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(f"{SEBRAE_URL}/login", wait_until="domcontentloaded")
        await asyncio.sleep(2)

        await page.fill("input[name='username'], input[type='email'], #username", SEBRAE_USER)
        await page.fill("input[name='password'], input[type='password'], #password", SEBRAE_PASS)
        await page.click("button[type='submit']")

        # Aguarda cookie aparecer (max 30s)
        for _ in range(30):
            cookies = await context.cookies()
            crm = next((c for c in cookies if "crm" in c["name"].lower()), None)
            if crm:
                val = urllib.parse.unquote(crm["value"])
                data = json.loads(val)
                await browser.close()
                return data.get("appKey"), data.get("token")
            await asyncio.sleep(1)

        await browser.close()
        return None, None
