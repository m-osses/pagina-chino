"""Busca fotos candidatas en Openverse (Flickr, Wikimedia y otras fuentes con licencia libre)
para las fichas de la Guía de Gabriel que todavía no tienen foto.

No modifica la página: guarda hasta 3 candidatas por lugar en candidatas/ junto con
candidatas.json (autor, licencia, enlace) para revisarlas a mano. Las elegidas se anotan en
scripts/fotos_excepciones.json como {"openverse": "<id>"} y fotos_lugares.py las descarga.

Solo acepta licencias que permiten uso comercial y modificación: CC BY, CC BY-SA, CC0 y
dominio público. Las NC (no comercial) y ND (sin derivadas) quedan afuera.
"""
import html
import io
import json
import os
import re
import sys
import time
import unicodedata

import requests
from PIL import Image, ImageDraw, ImageFont

INDEX = os.environ.get("INDEX", "index.html")
OUT = "candidatas"
API = "https://api.openverse.org/v1/images/"
UA = "CasaDoctaGuia/1.0 (https://www.doctachacabuco52.com.ar; chacabuco52cordoba@gmail.com)"
LICENCIAS = "by,by-sa,cc0,pdm"
POR_LUGAR = 3
PAUSA = 13  # el acceso anónimo admite pocas consultas por minuto

S = requests.Session()
S.headers["User-Agent"] = UA


def norm(t):
    t = unicodedata.normalize("NFKD", (t or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def strip_tags(t):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", t or ""))).strip()


def slug(t):
    return re.sub(r"[^a-z0-9]+", "-", norm(t)).strip("-")[:60] or "lugar"


STOP = {"de", "del", "la", "las", "el", "los", "y", "en", "a", "al", "cordoba", "centro"}


def palabras(t):
    return {w for w in re.findall(r"[a-z0-9]{4,}", norm(t)) if w not in STOP}


def lugares_sin_foto(s):
    out = []
    for m in re.finditer(r"<article\b.*?</article>", s, re.S):
        a = m.group(0)
        fig = re.search(r'<figure class="g-photo-frame">(.*?)</figure>', a, re.S)
        if not fig:
            continue
        img = re.search(r"<img\b[^>]*>", fig.group(1)).group(0)
        if "src=" in img:
            continue
        lugar = strip_tags(re.search(r"<h4[^>]*>(.*?)</h4>", a, re.S).group(1))
        direccion = re.search(r'<div class="g-address">(.*?)</div>', a, re.S)
        out.append((lugar, strip_tags(direccion.group(1)) if direccion else ""))
    # sin duplicados (un lugar puede repetirse en dos categorías)
    vistos, unicos = set(), []
    for l, d in out:
        if l not in vistos:
            vistos.add(l)
            unicos.append((l, d))
    return unicos


def consultar(q):
    for intento in range(5):
        r = S.get(API, params={"q": q, "license": LICENCIAS, "page_size": 50, "mature": "false"}, timeout=40)
        if r.status_code == 429:
            espera = int(r.headers.get("Retry-After", 60))
            print(f"  429, espero {espera}s")
            time.sleep(min(espera, 300))
            continue
        r.raise_for_status()
        time.sleep(PAUSA)
        return r.json().get("results", [])
    return []


def licencia_ok(res):
    lic = norm(res.get("license", ""))
    return lic in {"by", "by-sa", "cc0", "pdm"}


ESPANA = ("espana", "spain", "andalucia", "andalusia", "mezquita-catedral", "mosque-cathedral", "sevilla", "granada")


def relevante(res, lugar):
    texto = " ".join([res.get("title") or "", " ".join(t.get("name", "") for t in res.get("tags") or [])])
    t = norm(texto)
    if any(e in t for e in ESPANA):
        return False
    return ("cordoba" in t or "argentina" in t) and bool(palabras(lugar) & palabras(texto))


def main():
    s = open(INDEX, encoding="utf-8").read()
    os.makedirs(OUT, exist_ok=True)
    faltan = lugares_sin_foto(s)
    print(f"Lugares sin foto: {len(faltan)}")
    resultado = []
    for lugar, direccion in faltan:
        base = re.sub(r"\s*·.*$", "", lugar)
        # Openverse exige todas las palabras: consultas cortas y el filtro de Córdoba lo hacemos nosotros
        nombre = re.sub(r"\b(de|del)\s+córdoba\b", "", base, flags=re.I).strip()
        consultas = [nombre, f"{nombre} cordoba"]
        elegidas, ids = [], set()
        for q in consultas:
            crudos = consultar(q)
            print(f"  [{q}] {len(crudos)} resultados con licencia libre")
            for res in crudos[:6]:
                tags = ",".join(t.get("name", "") for t in (res.get("tags") or [])[:6])
                print(f"     - {res.get('source')} | {res.get('license')} | {(res.get('title') or '')[:60]} | tags: {tags[:70]} | rel={relevante(res, lugar)}")
            for res in crudos:
                if res["id"] in ids or not licencia_ok(res) or not relevante(res, lugar):
                    continue
                ids.add(res["id"])
                elegidas.append(res)
                if len(elegidas) >= POR_LUGAR:
                    break
            if len(elegidas) >= POR_LUGAR:
                break
        item = {"lugar": lugar, "direccion": direccion, "candidatas": []}
        for n, res in enumerate(elegidas, 1):
            archivo = f"{OUT}/{slug(lugar)}-{n}.jpg"
            try:
                r = S.get(res["url"], timeout=60)
                r.raise_for_status()
                im = Image.open(io.BytesIO(r.content)).convert("RGB")
                im.thumbnail((640, 640))
                im.save(archivo, quality=82)
            except Exception as e:  # noqa: BLE001 - una foto rota no debe frenar el resto
                print(f"  no se pudo bajar {res['url']}: {e.__class__.__name__}")
                continue
            item["candidatas"].append({
                "n": n, "archivo": archivo, "openverse": res["id"], "titulo": res.get("title"),
                "autor": res.get("creator"), "licencia": f"CC {res['license'].upper()} {res.get('license_version') or ''}".strip()
                if res["license"] not in ("cc0", "pdm") else res["license"].upper(),
                "fuente": res.get("source") or res.get("provider"), "pagina": res.get("foreign_landing_url"),
            })
        print(f"{lugar}: {len(item['candidatas'])} candidatas")
        resultado.append(item)
    json.dump(resultado, open(f"{OUT}/candidatas.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # hoja de contactos: una fila por lugar
    con = [i for i in resultado if i["candidatas"]]
    if con:
        tw, th, cap, izq = 260, 180, 46, 230
        hoja = Image.new("RGB", (izq + POR_LUGAR * tw, len(con) * (th + cap)), "white")
        d = ImageDraw.Draw(hoja)
        try:
            f = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
        except OSError:
            f = ImageFont.load_default()
        for r_, it in enumerate(con):
            y = r_ * (th + cap)
            d.text((8, y + 8), it["lugar"][:30], fill="black", font=f)
            d.text((8, y + 26), it["direccion"][:30], fill="#666", font=f)
            for c in it["candidatas"]:
                x = izq + (c["n"] - 1) * tw
                im = Image.open(c["archivo"])
                im.thumbnail((tw - 8, th - 8))
                hoja.paste(im, (x + 4, y + 4))
                d.text((x + 4, y + th), f"#{c['n']} {c['fuente']} · {c['licencia']}"[:38], fill="#8a5a00", font=f)
                d.text((x + 4, y + th + 16), (c["titulo"] or "")[:38], fill="#444", font=f)
        hoja.save(f"{OUT}/hoja-candidatas.jpg", quality=82)
    sin = [i["lugar"] for i in resultado if not i["candidatas"]]
    print(f"Con candidatas: {len(con)} | sin resultados: {len(sin)}: {', '.join(sin)}")


if __name__ == "__main__":
    main()
