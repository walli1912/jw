"""Pruebas sin Internet del catálogo y de las garantías de publicación."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import generar_lista_enriquecida4 as jw


def video(clave, titulo='Video, parte 1', calidades=('720p', '1080p')):
    return {'naturalKey': clave, 'title': titulo, 'files': [
        {'label': q, 'progressiveDownloadURL': f'https://example.org/{clave}_{q}.mp4'} for q in calidades]}


def respuesta(clave, nombre, media=None, hijos=None):
    cat = {'key': clave, 'name': nombre}
    data = {'category': cat}
    if media is not None:
        cat['media'] = media
        data['pagination'] = {'totalCount': len(media), 'offset': 0}
    if hijos is not None:
        cat['subcategories'] = [{'key': k, 'name': n} for k, n in hijos]
    return data


def documentos():
    return {
        'VideoOnDemand': respuesta('VideoOnDemand', 'Categorías', hijos=[('Programas', 'Programas'), ('Series', 'Series'), ('VODAudioDescriptions', 'Audiodescripciones')]),
        'VODAudioDescriptions': respuesta('VODAudioDescriptions', 'Videos con audiodescripciones', [video('ad', 'Video (audiodescripción)')]),
        'Programas': respuesta('Programas', 'Programas', hijos=[('2026Convention', 'Asamblea 2026'), ('2025Convention', 'Asamblea 2025')]),
        '2026Convention': respuesta('2026Convention', 'Asamblea 2026', [video('nuevo')]),
        '2025Convention': respuesta('2025Convention', 'Asamblea 2025', [video('viejo')]),
        'Series': respuesta('Series', 'Series', [video('episodio'), video('episodio'), video('ad', 'Video (audiodescripción)')]),
    }


class CatalogoTest(unittest.TestCase):
    def test_calidad_y_filtros(self):
        self.assertEqual(jw.elegir_medio(video('v'))['label'], '1080p')
        self.assertEqual(jw.elegir_medio(video('v', calidades=('720p',)))['label'], '720p')
        self.assertIsNone(jw.elegir_medio(video('v', calidades=('480p',))))
        self.assertTrue(jw.es_audiodescripcion('Audio descripción'))
        v = video('v');v['files'][1]['subtitled'] = True
        self.assertEqual(jw.elegir_medio(v)['label'], '720p')

    def test_arbol_y_orden(self):
        with patch.object(jw, 'consultar', side_effect=lambda k, offset=0, **kw: copy.deepcopy(documentos()[k])):
            docs = jw.descargar_arbol('VideoOnDemand', workers=2)
        self.assertIn('VODAudioDescriptions', docs)
        nodos = jw.preparar_catalogo(docs, 'VideoOnDemand')
        self.assertEqual(nodos['VideoOnDemand']['count'], 4)
        self.assertEqual(len(nodos['Series']['videos']), 1)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            jw.escribir_listas(p, nodos, 'VideoOnDemand', 'https://example.org/')
            raiz = (p/'inicio.m3u').read_text()
            self.assertIn('type="playlist"', raiz)
            self.assertNotIn('tvg-logo', raiz)
            self.assertIn('Cantidad: 2', raiz)
            programas = (p/'listas/Programas.m3u').read_text()
            self.assertLess(programas.index('Asamblea 2026'), programas.index('Asamblea 2025'))
            self.assertEqual((p/'listas/Series.m3u').read_text().count('#EXTINF'), 1)
            for lista in p.rglob('*.m3u'):
                for linea in lista.read_text().splitlines():
                    if linea.startswith('#EXTINF'):
                        self.assertEqual(linea.count(','), 1)

    def test_catorce_carpetas_en_orden_y_audiodescripciones(self):
        claves = [k for k, _ in jw.CATEGORIAS_PRINCIPALES]
        docs = {'VideoOnDemand': respuesta('VideoOnDemand', 'Categorías', hijos=[(k, 'Nombre viejo') for k in reversed(claves)])}
        for k in claves:
            docs[k] = respuesta(k, 'Nombre viejo', [video(k, 'Título (audiodescripción)' if k == 'VODAudioDescriptions' else k)])
        nodos = jw.preparar_catalogo(docs, 'VideoOnDemand')
        self.assertEqual(nodos['VideoOnDemand']['children'], claves)
        self.assertEqual(len(nodos['VODAudioDescriptions']['videos']), 1)
        self.assertEqual([nodos[k]['name'] for k in claves], [n for _, n in jw.CATEGORIAS_PRINCIPALES])
        with tempfile.TemporaryDirectory() as d:
            jw.escribir_listas(Path(d), nodos, 'VideoOnDemand', 'https://example.org/')
            texto = (Path(d)/'inicio.m3u').read_text()
            self.assertEqual(texto.count('#EXTINF'), 14)
            titulos = [linea.split(',', 1)[1] for linea in texto.splitlines() if linea.startswith('#EXTINF')]
            self.assertEqual(titulos, [n for _, n in jw.CATEGORIAS_PRINCIPALES])

    def test_audiodescripciones_solo_en_su_seccion(self):
        docs = documentos()
        # La misma versión AD aparece además en una carpeta común, sin marca en el título.
        ad_sin_marca = video('ad_sin_marca', 'Título sin advertencia')
        docs['VODAudioDescriptions']['category']['media'].append(ad_sin_marca)
        docs['Series']['category']['media'].append(copy.deepcopy(ad_sin_marca))
        docs['Series']['category']['subcategories'] = [{'key': 'VODAudioDescriptions'}]
        nodos = jw.preparar_catalogo(docs, 'VideoOnDemand')
        self.assertEqual([v['id'] for v in nodos['Series']['videos']], ['episodio'])
        self.assertEqual({v['id'] for v in nodos['VODAudioDescriptions']['videos']}, {'ad', 'ad_sin_marca'})
        self.assertNotIn('VODAudioDescriptions', nodos['Series']['children'])
        self.assertIn('VODAudioDescriptions', nodos['VideoOnDemand']['children'])

    def test_estructura_completa(self):
        parcial = respuesta('P', 'Programas', hijos=[('2026Convention', 'Asamblea 2026')])
        completa = respuesta('P', 'Programas', hijos=[('2026Convention', 'Asamblea 2026'), ('2025Convention', 'Asamblea 2025')])
        with patch.object(jw, 'consultar', side_effect=[parcial, completa]) as mock:
            self.assertEqual(len(jw.descargar_categoria('P')['category']['subcategories']), 2)
            mock.assert_any_call('P', solo_estructura=True)

    def test_paginacion(self):
        a = respuesta('S', 'Serie', [video('uno')]);a['pagination']['totalCount'] = 2
        b = respuesta('S', 'Serie', [video('dos')]);b['pagination'].update(totalCount=2, offset=1)
        with patch.object(jw, 'consultar', side_effect=[copy.deepcopy(a), copy.deepcopy(b)]):
            self.assertEqual(len(jw.descargar_categoria('S')['category']['media']), 2)
        with patch.object(jw, 'consultar', side_effect=[copy.deepcopy(a), respuesta('S', 'Serie', [])]):
            with self.assertRaises(ValueError):jw.descargar_categoria('S')

    def test_logs_y_error_conserva_archivos(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            with patch.object(jw, 'descargar_arbol', return_value=documentos()):
                codigo,_ = jw.ejecutar(p)
            self.assertEqual(codigo, 0)
            anterior = json.loads((p/'catalogo_estado.json').read_text())
            self.assertTrue(jw.comparar(None, anterior)['initial'])
            self.assertEqual(jw.comparar(anterior, anterior)['added'], [])
            nuevo = copy.deepcopy(anterior)
            nuevo['videos']['incorporado'] = {'title': 'Nuevo', 'url': 'https://example.org/a', 'quality': '1080p', 'categories': ['Series']}
            del nuevo['videos']['viejo']
            nuevo['videos']['episodio']['url'] += '?nuevo=1'
            cambios = jw.comparar(anterior, nuevo)
            self.assertEqual(cambios['added'], ['incorporado'])
            self.assertEqual(cambios['removed'], ['viejo'])
            self.assertEqual(cambios['changed'], ['episodio'])
            antes = {f.relative_to(p):f.read_bytes() for f in p.rglob('*') if f.is_file() and 'historial' not in f.parts}
            with patch.object(jw, 'descargar_arbol', side_effect=OSError('Sin Internet')):
                codigo,_ = jw.ejecutar(p)
            self.assertEqual(codigo, 1)
            for f, contenido in antes.items():self.assertEqual((p/f).read_bytes(), contenido)
            self.assertEqual(len(list((p/'historial').glob('*.txt'))), 2)
            self.assertFalse(list(p.glob('.jw-preparando-*')))

    def test_ciclo_y_vacio(self):
        docs = documentos();docs['Series']['category']['subcategories'] = [{'key':'VideoOnDemand','name':'Categorías'}]
        with self.assertRaises(ValueError):jw.preparar_catalogo(docs, 'VideoOnDemand')
        with self.assertRaises(ValueError):jw.preparar_catalogo({'VideoOnDemand':respuesta('VideoOnDemand','Categorías',[])},'VideoOnDemand')


if __name__ == '__main__':
    unittest.main()
