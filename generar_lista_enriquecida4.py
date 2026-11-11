import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlparse


RESOLUCION = re.compile(r"(?:720p|1080p)", re.IGNORECASE)
MARCA_RESOLUCION = re.compile(r"_(?:r?720p|r?1080p)$", re.IGNORECASE)


def atributo_m3u(valor):
    """Evita que comillas o saltos de línea rompan los atributos M3U."""
    return str(valor or "").replace('"', "'").replace("\r", " ").replace("\n", " ").strip()


def candidatas_imagen(item):
    imagenes = item.get("images") or {}
    candidatas = []
    # Probamos variantes de la misma portada; algunas URL individuales pueden estar rechazadas.
    for formato, tamanos in (("wss", ("sm", "lg")), ("pnr", ("lg", "sm")),
                             ("lss", ("lg",)), ("lsr", ("xl",)), ("sqr", ("lg",))):
        bloque = imagenes.get(formato) or {}
        for tamano in tamanos:
            url = bloque.get(tamano)
            if url and url not in candidatas:
                candidatas.append(url)
    return candidatas


def elegir_imagen(item):
    candidatas = candidatas_imagen(item)
    # Este recurso de JW devuelve AccessDenied para las variantes wss.
    # El mismo registro ofrece una imagen horizontal alternativa (lss).
    if item.get("naturalKey", "").lower() == "pub-jwb-141_s_1_video":
        imagen_lss = (item.get("images", {}).get("lss", {}) or {}).get("lg")
        if imagen_lss:
            return imagen_lss
    # No se consulta la red por cada portada: con cientos de videos era muy lento.
    # Se usan las URL declaradas por JW en el JSON, priorizando wss.sm.
    return candidatas[0] if candidatas else ""


# Ejecutable en Windows y en GitHub Actions. Solo usa la biblioteca estándar.
import argparse
import copy
import hashlib
import html
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.parse import urlencode, quote

BASE = 'https://b.jw-cdn.org/apis/mediator/v1/categories/S/'
CATEGORIA_PRINCIPAL = 'VideoOnDemand'
URL_PUBLICA = 'https://walli1912.github.io/jw/'
CALIDADES_PREFERIDAS = (1080, 720)
PREFIJO_JSON = 'JW_AUTO_'


# Orden de lectura de la cuadrícula de JW.ORG: izquierda a derecha, fila por fila.
CATEGORIAS_PRINCIPALES = (
    ('VODStudio', 'JW Broadcasting'),
    ('VODChildren', 'Niños'),
    ('VODTeenagers', 'Jóvenes'),
    ('VODFamily', 'Familia'),
    ('VODProgramsEvents', 'Programas espirituales'),
    ('VODOurActivities', 'Qué hacemos'),
    ('VODMinistry', 'Nuestras reuniones y ministerio'),
    ('VODOurOrganization', 'Nuestra organización'),
    ('VODBible', 'La Biblia'),
    ('VODMovies', 'Producciones audiovisuales'),
    ('VODSeries', 'Series'),
    ('VODMusicVideos', 'Música'),
    ('VODIntExp', 'Entrevistas y experiencias'),
    ('VODAudioDescriptions', 'Videos con audiodescripciones'),
)


def es_audiodescripcion(texto):
    texto = ''.join(c for c in unicodedata.normalize('NFKD', str(texto or ''))
                    if not unicodedata.combining(c)).casefold()
    return bool(re.search(r'audio[\s_-]*descri', texto))


def clave_valida(clave):
    if not isinstance(clave, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', clave):
        raise ValueError('Identificador de categoría inválido')
    return clave


def consultar(clave, offset=0, solo_estructura=False):
    clave_valida(clave)
    parametros = {'detailed': 1, 'offset': offset, 'limit': 500, 'clientType': 'www'}
    if solo_estructura:
        parametros['mediaLimit'] = 0
    url = BASE + quote(clave, safe='') + '?' + urlencode(parametros)
    for intento in range(3):
        try:
            req = Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Cache-Control': 'no-cache'})
            with urlopen(req, timeout=45) as respuesta:
                data = json.loads(respuesta.read().decode('utf-8-sig'))
            if data['category']['key'] != clave:
                raise ValueError('Categoría inesperada en la respuesta: ' + clave)
            return data
        except Exception:
            if intento == 2:
                raise
            time.sleep(2 ** intento)


def descargar_categoria(clave):
    data = consultar(clave)
    cat = data['category']
    if 'media' not in cat and 'subcategories' not in cat:
        raise ValueError('Catálogo sin medios ni subcategorías: ' + clave)
    if not isinstance(cat.get('media', []), list) or not isinstance(cat.get('subcategories', []), list):
        raise ValueError('Estructura de catálogo inesperada: ' + clave)
    videos = list(cat.get('media') or [])
    pag = data.get('pagination')
    if pag is not None:
        total = int(pag['totalCount'])
        if total < 0 or int(pag['offset']) != 0:
            raise ValueError('Paginación inesperada: ' + clave)
        while len(videos) < total:
            offset = len(videos)
            pagina = consultar(clave, offset)
            bloque = pagina['category'].get('media') or []
            if int(pagina['pagination']['offset']) != offset or not bloque:
                raise ValueError('Catálogo incompleto: ' + clave)
            if int(pagina['pagination']['totalCount']) != total:
                raise ValueError('El catálogo cambió durante la descarga: ' + clave)
            videos.extend(bloque)
        if len(videos) != total:
            raise ValueError('Cantidad de registros inconsistente: ' + clave)
        cat['media'] = videos
        data['pagination'] = {'totalCount': len(videos), 'offset': 0, 'limit': max(len(videos), 1)}
    elif videos:
        raise ValueError('Falta la paginación: ' + clave)
    if cat.get('subcategories'):
        # Sin mediaLimit=0 JW recorta algunas listas de subcategorías (p. ej., a seis).
        estructura = consultar(clave, solo_estructura=True)['category']
        hijos = estructura.get('subcategories')
        if not isinstance(hijos, list):
            raise ValueError('Falta la estructura completa de subcategorías: ' + clave)
        cat['subcategories'] = hijos
    return data


def descargar_arbol(raiz, workers=4):
    """Descarga en paralelo; el orden visual se toma del JSON, no del orden de llegada."""
    documentos = {}
    solicitadas = set()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pendientes = {}

        def agregar(clave):
            clave_valida(clave)
            if clave not in solicitadas:
                solicitadas.add(clave)
                pendientes[pool.submit(descargar_categoria, clave)] = clave

        agregar(raiz)
        while pendientes:
            terminadas, _ = wait(pendientes, return_when=FIRST_COMPLETED)
            for futuro in terminadas:
                clave = pendientes.pop(futuro)
                try:
                    data = futuro.result()
                except Exception as error:
                    for otro in pendientes:
                        otro.cancel()
                    raise RuntimeError(f'No se pudo actualizar {clave}: {error}') from error
                cat = data['category']
                documentos[clave] = data
                print(f'[{len(documentos)}/{len(solicitadas)}] {cat.get("name", clave)}', flush=True)
                for sub in cat.get('subcategories') or []:
                    agregar(sub['key'])
    if raiz not in documentos:
        raise ValueError('No se obtuvo el catálogo principal')
    return documentos


def elegir_medio(video):
    candidatos = []
    for medio in video.get('files') or []:
        url = medio.get('progressiveDownloadURL') or ''
        if not url or medio.get('subtitled') or urlparse(url).scheme not in ('http', 'https'):
            continue
        if any(x in url.lower() for x in ('token=', 'signature=', 'index_', '.vtt')):
            continue
        match = re.search(r'(720|1080)p', str(medio.get('label', '')), re.I)
        if not match:
            match = re.search(r'(720|1080)p', urlparse(url).path, re.I)
        if match:
            candidatos.append((CALIDADES_PREFERIDAS.index(int(match.group(1))), url, medio))
    return min(candidatos, key=lambda x: (x[0], x[1]))[2] if candidatos else None


def identidad(video, url):
    return str(video.get('naturalKey') or video.get('guid') or
               video.get('languageAgnosticNaturalKey') or hashlib.sha256(url.encode()).hexdigest())


def preparar_catalogo(documentos, raiz):
    # La pertenencia al árbol oficial AD también identifica videos sin marca en el título.
    categorias_ad = set()

    def marcar_ad(clave):
        if clave in categorias_ad or clave not in documentos:
            return
        categorias_ad.add(clave)
        for sub in documentos[clave]['category'].get('subcategories') or []:
            marcar_ad(sub['key'])

    marcar_ad('VODAudioDescriptions')
    ids_ad = set()
    for clave in categorias_ad:
        for video in documentos[clave]['category'].get('media') or []:
            medio = elegir_medio(video)
            if medio:
                ids_ad.add(identidad(video, medio['progressiveDownloadURL']))
    nodos = {}
    for clave, data in documentos.items():
        cat = data['category']
        seleccion = []
        vistos, urls = set(), set()
        for v in cat.get('media') or []:
            medio = elegir_medio(v)
            if medio is None:
                continue
            url = medio['progressiveDownloadURL']
            ident = identidad(v, url)
            if clave not in categorias_ad and (es_audiodescripcion(v.get('title')) or ident in ids_ad):
                continue
            if ident in vistos or url in urls:
                continue
            titulo = str(v.get('title') or '').strip()
            if not titulo:
                raise ValueError('Hay un video sin título en ' + clave)
            vistos.add(ident)
            urls.add(url)
            seleccion.append({'id': ident, 'title': titulo, 'url': url,
                              'quality': str(medio.get('label') or ''), 'logo': elegir_imagen(v)})
        hijos = [s['key'] for s in cat.get('subcategories') or [] if s['key'] in documentos]
        if clave not in categorias_ad:
            # No se ofrecen accesos a las secciones AD desde otras carpetas.
            hijos = [h for h in hijos if h not in categorias_ad or
                     (clave == CATEGORIA_PRINCIPAL and h == 'VODAudioDescriptions')]
        nodos[clave] = {'name': str(cat.get('name') or clave), 'children': hijos, 'videos': seleccion}
    if raiz == CATEGORIA_PRINCIPAL:
        nombres = dict(CATEGORIAS_PRINCIPALES)
        presentes = set(nodos[raiz]['children'])
        ordenados = [k for k, _ in CATEGORIAS_PRINCIPALES if k in presentes]
        # No se pierden categorías futuras: se agregan al final hasta definir su orden.
        ordenados.extend(k for k in nodos[raiz]['children'] if k not in nombres)
        nodos[raiz]['children'] = ordenados
        for clave in ordenados:
            if clave in nombres:
                nodos[clave]['name'] = nombres[clave]
    contenidos = {}

    def contar(clave, ruta=frozenset()):
        if clave in ruta:
            raise ValueError('Referencia circular en las categorías: ' + clave)
        if clave not in contenidos:
            ids = {v['id'] for v in nodos[clave]['videos']}
            for hijo in nodos[clave]['children']:
                ids.update(contar(hijo, ruta | {clave}))
            contenidos[clave] = ids
        return contenidos[clave]

    contar(raiz)
    if not contenidos[raiz]:
        raise ValueError('El catálogo no contiene videos HD válidos; se conserva la lista anterior')
    # Las carpetas vacías no se publican, pero su JSON se conserva.
    for clave in nodos:
        contar(clave)
        nodos[clave]['children'] = [h for h in nodos[clave]['children'] if contenidos[h]]
        nodos[clave]['count'] = len(contenidos[clave])
    return nodos


def inventario(nodos, raiz):
    videos = {}
    categorias = {}
    recorridas = set()

    def visitar(clave, padres=()):
        if clave in recorridas:
            return
        recorridas.add(clave)
        nodo = nodos[clave]
        ruta = padres + (nodo['name'],)
        nombre = ' / '.join(ruta[1:] if len(ruta) > 1 else ruta)
        categorias[clave] = {'name': nodo['name'], 'path': nombre}
        for v in nodo['videos']:
            item = videos.setdefault(v['id'], {k: v[k] for k in ('title', 'url', 'quality')})
            item.setdefault('categories', []).append(nombre)
        for h in nodo['children']:
            visitar(h, ruta)

    visitar(raiz)
    for v in videos.values():
        v['categories'] = sorted(set(v['categories']))
    return {'root': raiz, 'categories': categorias, 'videos': videos}


def comparar(anterior, actual):
    if anterior is None:
        return {'initial': True, 'added': [], 'removed': [], 'changed': [],
                'new_categories': list(actual['categories']), 'removed_categories': []}
    antes, ahora = anterior['videos'], actual['videos']
    return {'initial': False, 'added': sorted(ahora.keys() - antes.keys()),
            'removed': sorted(antes.keys() - ahora.keys()),
            'changed': sorted(k for k in ahora.keys() & antes.keys() if antes[k] != ahora[k]),
            'new_categories': sorted(actual['categories'].keys() - anterior['categories'].keys()),
            'removed_categories': sorted(anterior['categories'].keys() - actual['categories'].keys())}


def texto_seguro(texto):
    return atributo_m3u(texto).replace(',', ';')


def escribir_listas(destino, nodos, raiz, base_url):
    carpeta = destino / 'listas'
    carpeta.mkdir(parents=True, exist_ok=True)
    for clave, nodo in nodos.items():
        if not nodo['count']:
            continue
        lineas = ['#EXTM3U size="medium"']
        for hijo in nodo['children']:
            sub = nodos[hijo]
            lineas.extend([f'#EXTINF:0 type="playlist" description="Cantidad: {sub["count"]}",{texto_seguro(sub["name"])}',
                           '#EXTSIZE: Medium', base_url + 'listas/' + hijo + '.m3u'])
        for v in nodo['videos']:
            atributos = 'type="video"'
            if v['logo']:
                atributos += f' tvg-logo="{texto_seguro(v["logo"])}"'
            lineas.extend([f'#EXTINF:0 {atributos},{texto_seguro(v["title"])}', v['url']])
        texto = '\n'.join(lineas) + '\n'
        (carpeta / (clave + '.m3u')).write_text(texto, encoding='utf-8')
        if clave == raiz:
            (destino / 'inicio.m3u').write_text(texto, encoding='utf-8')
    (destino / '.nojekyll').write_text('')


def escribir_plana(destino, nodos):
    lineas = ['#EXTM3U']
    for nodo in nodos.values():
        for v in nodo['videos']:
            logo = f' tvg-logo="{texto_seguro(v["logo"])}"' if v['logo'] else ''
            lineas.extend([f'#EXTINF:-1 group-title="{texto_seguro(nodo["name"])}"{logo},{texto_seguro(v["title"])}', v['url']])
    (destino / 'lista_completa_smarttv.m3u').write_text('\n'.join(lineas) + '\n', encoding='utf-8-sig')


def guardar_json(destino, documentos):
    for clave, data in documentos.items():
        (destino / (PREFIJO_JSON + clave + '.json')).write_text(
            json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')


def registrar(carpeta, actual=None, anterior=None, cambios=None, error=None):
    ahora = datetime.now(timezone.utc)
    fecha = ahora.astimezone(timezone(timedelta(hours=-3))).strftime('%Y-%m-%d %H:%M:%S (Argentina)')
    nombre = ahora.strftime('%Y-%m-%d_%H-%M-%S_%f')
    historial = carpeta / 'historial'
    historial.mkdir(parents=True, exist_ok=True)
    lineas = [f'Actualización: {fecha}', '']
    datos = {'utc': ahora.isoformat(), 'success': error is None}
    if error:
        datos['error'] = str(error)
        lineas.extend(['ERROR: ' + str(error), 'Se conserva el último catálogo publicado válido.'])
    else:
        datos.update(cambios)
        datos['total_videos'] = len(actual['videos'])
        datos['total_categories'] = len(actual['categories'])
        lineas.extend([f'Videos distintos: {len(actual["videos"])}',
                       f'Categorías: {len(actual["categories"])}'])
        if cambios['initial']:
            lineas.append('Primera ejecución: se guardó el catálogo de referencia para futuras comparaciones.')
        for campo, titulo, fuente in [('added', 'VIDEOS INCORPORADOS A LA LISTA', actual['videos']),
                                     ('removed', 'VIDEOS RETIRADOS', (anterior or {}).get('videos', {})),
                                     ('changed', 'VIDEOS MODIFICADOS (título; enlace; calidad o categorías)', actual['videos'])]:
            lineas.extend(['', f'{titulo}: {len(cambios[campo])}'])
            for ident in cambios[campo]:
                v = fuente[ident]
                lineas.append('- ' + v['title'] + ' | ' + ' ; '.join(v['categories']))
        for campo, titulo, fuente in [('new_categories', 'CATEGORÍAS NUEVAS', actual['categories']),
                                     ('removed_categories', 'CATEGORÍAS RETIRADAS', (anterior or {}).get('categories', {}))]:
            lineas.extend(['', f'{titulo}: {len(cambios[campo])}'])
            lineas.extend('- ' + fuente[k]['path'] for k in cambios[campo])
    texto = '\n'.join(lineas) + '\n'
    (historial / (nombre + '.txt')).write_text(texto, encoding='utf-8')
    (historial / (nombre + '.json')).write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding='utf-8')
    enlaces = ''.join(f'<li><a href="{p.name}">{html.escape(p.stem)}</a></li>'
                      for p in sorted(historial.glob('*.txt'), reverse=True))
    (historial / 'index.html').write_text('<!doctype html><html lang="es"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width"><title>Historial JW</title>'
        '<body><h1>Historial de actualizaciones</h1><p>Horarios de los nombres de archivos: UTC. '
        'Cada registro indica también la hora de Argentina.</p><ul>' + enlaces + '</ul></body></html>', encoding='utf-8')
    print(texto, flush=True)
    return texto


def ejecutar(carpeta, base_url=URL_PUBLICA, raiz=CATEGORIA_PRINCIPAL, workers=4):
    carpeta = Path(carpeta).resolve()
    carpeta.mkdir(parents=True, exist_ok=True)
    base_url = base_url.rstrip('/') + '/'
    if urlparse(base_url).scheme not in ('http', 'https') or not urlparse(base_url).netloc:
        raise ValueError('La URL pública debe ser http o https')
    staging = Path(tempfile.mkdtemp(prefix='.jw-preparando-', dir=carpeta))
    try:
        estado = carpeta / 'catalogo_estado.json'
        anterior = json.loads(estado.read_text(encoding='utf-8')) if estado.exists() else None
        documentos = descargar_arbol(raiz, workers)
        nodos = preparar_catalogo(documentos, raiz)
        actual = inventario(nodos, raiz)
        cambios = comparar(anterior, actual)
        escribir_listas(staging, nodos, raiz, base_url)
        escribir_plana(staging, nodos)
        guardar_json(staging, documentos)
        (staging / 'catalogo_estado.json').write_text(json.dumps(actual, ensure_ascii=False, indent=2), encoding='utf-8')
        # Todo se valida y genera antes de reemplazar los archivos publicados.
        nombres_anteriores = set()
        if anterior:
            nombres_anteriores = set(anterior['categories']) - set(actual['categories'])
        for archivo in staging.rglob('*'):
            if archivo.is_file():
                destino = carpeta / archivo.relative_to(staging)
                destino.parent.mkdir(parents=True, exist_ok=True)
                os.replace(archivo, destino)
        for clave in nombres_anteriores:
            clave_valida(clave)
            for archivo in [carpeta / 'listas' / (clave + '.m3u'), carpeta / (PREFIJO_JSON + clave + '.json')]:
                archivo.unlink(missing_ok=True)
        resumen = registrar(carpeta, actual, anterior, cambios)
        print('Lista permanente:', base_url + 'inicio.m3u')
        print('Historial:', base_url + 'historial/')
        return 0, resumen
    except Exception as error:
        resumen = registrar(carpeta, error=error)
        return 1, resumen
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description='Actualiza el catálogo JW con carpetas para SS IPTV.')
    parser.add_argument('--salida', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--base-url', default=os.environ.get('JW_BASE_URL', URL_PUBLICA))
    parser.add_argument('--raiz', default=CATEGORIA_PRINCIPAL)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.workers <= 8:
        parser.error('--workers debe estar entre 1 y 8')
    print('Actualizando JW. Se conservan los botones medianos sin portada, las miniaturas de videos y 1080p primero.', flush=True)
    codigo, resumen = ejecutar(args.salida, args.base_url, args.raiz, args.workers)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
            f.write('```text\n' + resumen.replace('```', "'''") + '\n```\n')
    if sys.stdin.isatty() and not os.environ.get('CI'):
        input('Presioná Enter para cerrar...')
    return codigo


if __name__ == '__main__':
    sys.exit(main())
