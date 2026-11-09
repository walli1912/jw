# Catálogo JW para SS IPTV

Navegación por carpetas y subcarpetas en el orden del catálogo de videos en español de JW.ORG. Los videos se reproducen desde los servidores de JW; este repositorio solo contiene código, listas y metadatos. Es un proyecto personal, no oficial de JW.ORG.

## En la TV

Agregar como lista externa en SS IPTV:

**https://walli1912.github.io/jw/inicio.m3u**

La dirección es permanente. Las carpetas se muestran como botones medianos, sin portada, con nombre y cantidad de videos distintos dentro de la sección. Los videos tienen miniaturas cuando JW las ofrece. Se prefiere 1080p y se utiliza 720p si no hay 1080p; no cambia de calidad durante la reproducción. Se excluyen audiodescripciones y se evita repetir resoluciones dentro de cada categoría. Un video puede pertenecer a distintas categorías, como en JW.

## Actualización automática y manual

El flujo **Actualizar catálogo JW** se ejecuta diariamente a las **09:23 de Argentina** (12:23 UTC). El horario de GitHub Actions es aproximado y puede haber demoras. GitHub puede suspender programaciones de repositorios públicos sin actividad durante 60 días; si sucede, se puede volver a habilitar desde Actions.

Para actualizar cuando se necesite: **Actions → Actualizar catálogo JW → Run workflow / Ejecutar flujo de trabajo → main → Run workflow**. No hace falta encender la PC. Las ejecuciones se encolan y no actualizan simultáneamente.

El flujo prueba el script, consulta todas las categorías, guarda los JSON `JW_AUTO_*.json` junto al script, genera los M3U, compara el inventario y registra las novedades. Luego guarda los cambios y solicita una publicación de GitHub Pages. Si falla alguna categoría, publica solo el historial de error y conserva las últimas listas válidas. El flujo no almacena credenciales personales: usa el token temporal del propio repositorio.

## Historial

**https://walli1912.github.io/jw/historial/**

Cada ejecución tiene un registro TXT legible y un JSON con sus resultados. Se registran videos nuevos, retirados y modificados, además de categorías nuevas/retiradas. La primera ejecución establece la referencia inicial; no presenta todos los videos anteriores como novedades. Los identificadores estables evitan considerar nuevo un video solo porque cambió su enlace o su calidad.

## Ejecutar en la PC

Descargar `generar_lista_enriquecida4.py` y ejecutarlo con Python 3.9 o posterior; no necesita instalar paquetes adicionales. Genera los JSON, el historial, las listas por carpeta y `lista_completa_smarttv.m3u` junto al script. Ejecutarlo en la PC **no sube automáticamente los archivos a GitHub**; para publicar sin pasos adicionales usar el botón de Actions.

También se puede ejecutar desde una consola:

```sh
python generar_lista_enriquecida4.py --salida ./salida
```

Para comprobar una sola categoría sin reemplazar el catálogo completo:

```sh
python generar_lista_enriquecida4.py --raiz DramasGoodNews --salida ./prueba
```

`--base-url` permite definir otro alojamiento; `--workers` controla las consultas simultáneas (4 por defecto). Las listas se regeneran con el catálogo actual; eliminar un JSON local no excluye categorías.

Las pruebas iniciales `vida.m3u` y `jesus.m3u` se conservan como respaldo y no forman parte de la actualización automática del catálogo completo.
