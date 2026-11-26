"""Descarga fotos libres de Wikimedia Commons para las fichas de la Guía de Gabriel.

Para cada ficha con <figure> en index.html:
  1. Busca un artículo de Wikipedia en español (data-wiki si existe; si no, búsqueda por título).
  2. Toma su imagen principal o, como respaldo, busca un archivo en Commons.
  3. Lee la licencia por la API y acepta solo licencias libres (CC BY, CC BY-SA, CC0, dominio público).
  4. Descarga una miniatura, la guarda en lugares/<slug>.webp y escribe el crédito en la ficha.
Si no hay foto libre, saca la imagen externa y deja el enlace "Ver fotografías del lugar".

Uso:  python scripts/fotos_lugares.py            (descarga y modifica index.html)
      python scripts/fotos_lugares.py --dry-run  (solo informa, no escribe nada)
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

INDEX = "index.html"
OUT_DIR = "lugares"
WIDTH = 800
UA = "CasaDoctaGuia/1.0 (https://www.doctachacabuco52.com.ar; chacabuco52cordoba@gmail.com)"
WIKI_API = "https://es.wikipedia.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
STOP = {"de", "del", "la", "las", "el", "los", "y", "en", "a", "al", "cordoba", "córdoba",
        "centro", "sede", "plaza", "parque", "museo", "iglesia", "calle", "av", "avenida", "bv"}

S = requests.Session()
S.headers["User-Agent"] = UA


def api(url, **params):
    params.update(format="json", formatversion=2)
    for intento in range(4):
        r = S.get(url, params=params, timeout=30)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 * (intento + 1))
            continue
        r.raise_for_status()
        time.sleep(0.3)  # ser amables con la API
        return r.json()
    r.raise_for_status()


def norm(t):
    t = unicodedata.normalize("NFKD", t.lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def palabras(t):
    return {w for w in re.findall(r"[a-z0-9]{4,}", norm(t)) if w not in {norm(x) for x in STOP}}


def slug(t):
    return re.sub(r"[^a-z0-9]+", "-", norm(t)).strip("-")[:60] or "lugar"


def strip_tags(t):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", t or ""))).strip()


def licencia_libre(nombre):
    n = norm(nombre)
    if re.search(r"\b(nc|nd)\b", n) or "non-commercial" in n or "fair use" in n:
        return False
    return bool(re.search(r"cc[ -]?by|cc0|public domain|dominio publico|\bpd\b|pd-", n))


def info_archivo(file_title):
    """Datos de un File: de Commons, o None si no es una foto libre utilizable."""
    if not file_title.lower().startswith("file:"):
        file_title = "File:" + file_title
    j = api(COMMONS_API, action="query", titles=file_title, prop="imageinfo",
            iiprop="url|extmetadata|mime", iiurlwidth=WIDTH)
    pages = j.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        return None
    ii = (pages[0].get("imageinfo") or [None])[0]
    if not ii or ii.get("mime") not in ("image/jpeg", "image/png", "image/webp"):
        return None
    meta = ii.get("extmetadata", {})
    lic = strip_tags(meta.get("LicenseShortName", {}).get("value", ""))
    if not licencia_libre(lic):
        return {"rechazada": lic or "sin licencia", "file": pages[0]["title"]}
    return {
        "file": pages[0]["title"],
        "thumb": ii.get("thumburl") or ii["url"],
        "page": ii.get("descriptionurl"),
        "autor": strip_tags(meta.get("Artist", {}).get("value", ""))[:80] or "Autor en Commons",
        "licencia": lic,
    }


def imagen_de_articulo(titulo):
    j = api(WIKI_API, action="query", titles=titulo, prop="pageimages", piprop="name", redirects=1)
    p = (j.get("query", {}).get("pages") or [{}])[0]
    return p.get("pageimage"), p.get("title")


EXCEPCIONES = {}
if os.path.exists("scripts/fotos_excepciones.json"):
    EXCEPCIONES = {k: v for k, v in json.load(open("scripts/fotos_excepciones.json", encoding="utf-8")).items()
                   if not k.startswith("_")}


def buscar(lugar, wiki):
    """Devuelve (info, metodo, articulo) o (None, motivo, None)."""
    if lugar in EXCEPCIONES:
        forzada = EXCEPCIONES[lugar]
        if not forzada:
            return None, "excluida a mano (scripts/fotos_excepciones.json)", None
        info = info_archivo(forzada)
        if info and "rechazada" not in info:
            return info, "elegida a mano", None
        return None, f"foto elegida a mano no utilizable: {forzada}", None
    intentos = []
    if wiki:
        intentos.append(("data-wiki", wiki))
    else:
        q = re.sub(r"\s*·.*$", "", lugar)  # 'Heladería · Chacabuco 290' -> 'Heladería'
        j = api(WIKI_API, action="query", list="search", srsearch=f"{q} Córdoba Argentina", srlimit=5)
        for hit in j.get("query", {}).get("search", []):
            if palabras(hit["title"]) & palabras(lugar):
                intentos.append(("búsqueda Wikipedia", hit["title"]))
    rechazos = []
    for metodo, art in intentos:
        img, art_real = imagen_de_articulo(art)
        if not img:
            continue
        info = info_archivo(img)
        if info and "rechazada" not in info:
            return info, metodo, art_real
        if info:
            rechazos.append(f"{info['file']} ({info['rechazada']})")
    # respaldo: búsqueda directa en Commons
    q = re.sub(r"\s*·.*$", "", lugar)
    j = api(COMMONS_API, action="query", list="search", srnamespace=6, srsearch=f"{q} Córdoba Argentina", srlimit=8)
    for hit in j.get("query", {}).get("search", []):
        if not palabras(hit["title"]) & palabras(lugar):
            continue
        info = info_archivo(hit["title"])
        if info and "rechazada" not in info:
            return info, "búsqueda Commons", None
        if info:
            rechazos.append(f"{info['file']} ({info['rechazada']})")
    return None, ("licencia no libre: " + "; ".join(rechazos)) if rechazos else "sin foto en Wikimedia", None


def a_webp(data, destino):
    im = Image.open(io.BytesIO(data)).convert("RGB")
    if im.width > WIDTH:
        im = im.resize((WIDTH, round(im.height * WIDTH / im.width)), Image.LANCZOS)
    im.save(destino, "WEBP", quality=78, method=6)
    return im


def hoja_revision(items, destino):
    """Hoja de contactos para revisar de un vistazo que cada foto sea del lugar correcto."""
    ok = [i for i in items if i.get("archivo")]
    cols, tw, th, cap = 4, 280, 190, 58
    filas = max(1, (len(ok) + cols - 1) // cols)
    hoja = Image.new("RGB", (cols * tw, filas * (th + cap)), "white")
    d = ImageDraw.Draw(hoja)
    try:
        f = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
    except OSError:
        f = ImageFont.load_default()
    for k, it in enumerate(ok):
        x, y = (k % cols) * tw, (k // cols) * (th + cap)
        im = Image.open(it["archivo"]).convert("RGB")
        im.thumbnail((tw - 10, th - 10))
        hoja.paste(im, (x + (tw - im.width) // 2, y + 5))
        d.text((x + 6, y + th), it["lugar"][:40], fill="black", font=f)
        d.text((x + 6, y + th + 16), it["metodo"][:40], fill="#7a5a1e", font=f)
        d.text((x + 6, y + th + 32), it["licencia"][:40], fill="#555", font=f)
    hoja.save(destino, quality=85)


def main():
    dry = "--dry-run" in sys.argv
    s = open(INDEX, encoding="utf-8").read()
    os.makedirs(OUT_DIR, exist_ok=True)
    items, usados = [], set()

    def procesar(m):
        art = m.group(0)
        fig = re.search(r'<figure class="g-photo-frame">(.*?)</figure>', art, re.S)
        if not fig:
            return art
        lugar = strip_tags(re.search(r"<h4[^>]*>(.*?)</h4>", art, re.S).group(1))
        img = re.search(r"<img\b[^>]*>", fig.group(1)).group(0)
        wiki = html.unescape(re.search(r'data-wiki="([^"]*)"', img).group(1)) if "data-wiki=" in img else None
        externa = re.search(r'src="(https?://[^"]+)"', img)
        try:
            info, metodo, articulo = buscar(lugar, wiki)
        except requests.RequestException as e:
            info, metodo, articulo = None, f"error de red: {e.__class__.__name__}", None
        it = {"lugar": lugar, "metodo": metodo, "articulo": articulo,
              "antes": "data-wiki" if wiki else ("externa" if externa else "sin foto")}
        items.append(it)
        alt = re.search(r'alt="([^"]*)"', img).group(1)
        if info:
            nombre = slug(lugar)
            while nombre in usados:
                nombre += "-2"
            usados.add(nombre)
            ruta = f"{OUT_DIR}/{nombre}.webp"
            it.update(archivo=ruta, file=info["file"], page=info["page"], autor=info["autor"], licencia=info["licencia"])
            if not dry:
                r = S.get(info["thumb"], timeout=60)
                r.raise_for_status()
                a_webp(r.content, ruta)
                time.sleep(0.3)
            nuevo_img = (f'<img alt="{alt}" class="g-place-img" src="{ruta}" '
                         f'loading="lazy" decoding="async">')
            credito = (f'<figcaption><a href="{html.escape(info["page"])}" rel="noopener" target="_blank">'
                       f'Foto: {html.escape(info["autor"])} · {html.escape(info["licencia"])} · Wikimedia Commons</a></figcaption>')
        else:
            # sin foto libre: se quita la imagen externa y queda el enlace a Google Maps
            nuevo_img = f'<img alt="{alt}" class="g-place-img" loading="lazy" decoding="async">'
            credito = ""
        cuerpo = fig.group(1).replace(img, nuevo_img, 1)
        cuerpo = re.sub(r"<figcaption>.*?</figcaption>", credito, cuerpo, flags=re.S)
        return art.replace(fig.group(1), cuerpo, 1)

    s2 = re.sub(r"<article\b.*?</article>", procesar, s, flags=re.S)
    con = [i for i in items if i.get("archivo")]
    sin = [i for i in items if not i.get("archivo")]
    print(f"Fichas con foto: {len(items)} | foto libre encontrada: {len(con)} | sin foto libre: {len(sin)}")
    for i in sin:
        print(f"  SIN FOTO: {i['lugar']} — {i['metodo']}")
    if dry:
        return
    open(INDEX, "w", encoding="utf-8").write(s2)
    json.dump(items, open(f"{OUT_DIR}/creditos.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if con:
        hoja_revision(items, "revision-fotos.jpg")
    # resumen para la pestaña Actions
    resumen = os.environ.get("GITHUB_STEP_SUMMARY")
    if resumen:
        repo, rama = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("RAMA", "")
        with open(resumen, "a", encoding="utf-8") as f:
            f.write(f"## Fotos de lugares\n\n{len(con)} de {len(items)} fichas con foto libre de Wikimedia.\n\n")
            f.write("| Lugar | Antes | Cómo se encontró | Foto | Licencia |\n|---|---|---|---|---|\n")
            for i in items:
                if i.get("archivo"):
                    url = f"https://raw.githubusercontent.com/{repo}/{rama}/{i['archivo']}"
                    f.write(f"| {i['lugar']} | {i['antes']} | {i['metodo']} | [![]({url})]({i['page']}) | {i['licencia']} · {i['autor']} |\n")
                else:
                    f.write(f"| {i['lugar']} | {i['antes']} | — | **sin foto libre** | {i['metodo']} |\n")


if __name__ == "__main__":
    main()
