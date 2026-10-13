# Estado del arte automático

Web que muestra cuántos artículos se publican cada año para distintas combinaciones de palabras clave en **OpenAlex**, **IEEE Xplore** y **Semantic Scholar**. Se actualiza sola cada semana con GitHub Actions y detecta los artículos nuevos.

```
config.yaml                  ← tus palabras clave, años y plataformas
collect.py                   ← consulta las APIs y genera los datos
.github/workflows/actualizar.yml  ← ejecución programada (cada lunes)
docs/index.html              ← la web (GitHub Pages)
docs/data/resultados.json    ← datos que lee la web (se regenera solo)
data/historico.csv           ← todos los snapshots, para tu análisis
data/novedades.json, data/vistos.json  ← control de artículos nuevos
```

## Puesta en marcha (unos 15 minutos)

1. **Consigue las claves gratuitas**
   - OpenAlex: crea una cuenta y copia la clave en <https://openalex.org/settings/api>. Desde febrero de 2026 es obligatoria; el plan gratuito da de sobra para este uso.
   - IEEE Xplore: regístrate en <https://developer.ieee.org> y pide una clave para la *Metadata Search API*.
   - Semantic Scholar (opcional): <https://www.semanticscholar.org/product/api>. Sin clave funciona, pero más lento.

2. **Crea un repositorio en GitHub** y sube todo el contenido de esta carpeta.

3. **Añade las claves como secretos**: *Settings → Secrets and variables → Actions → New repository secret* con los nombres `OPENALEX_API_KEY`, `IEEE_API_KEY` y, si la tienes, `S2_API_KEY`.

4. **Activa la web**: *Settings → Pages → Build and deployment → Deploy from a branch*, rama `main`, carpeta `/docs`. Tu web estará en `https://<usuario>.github.io/<repositorio>/`.

5. **Lanza la primera ejecución**: pestaña *Actions → Actualizar estado del arte → Run workflow*. A partir de ahí se ejecuta sola cada lunes.

> La primera ejecución solo registra los artículos existentes. La sección de **novedades** empieza a llenarse desde la segunda.

## Probar en tu ordenador

```bash
pip install -r requirements.txt
export OPENALEX_API_KEY=tu_clave IEEE_API_KEY=tu_clave
python collect.py                  # o: python collect.py --solo openalex
cd docs && python -m http.server   # abre http://localhost:8000
```

`python collect.py --demo` genera datos inventados para ver la web sin claves (el repositorio viene con ellos; la web los marca con un aviso).

## Personalizar

- **Palabras clave**: edita `config.yaml`. Una lista dentro de un término son sinónimos unidos con OR, por ejemplo `["digital twin", "digital twins"]`. Las combinaciones con AND se generan solas.
- **Frecuencia**: cambia el `cron` del workflow. Una vez por semana o al mes es suficiente: las bases tardan en indexar.
- **Límite de IEEE**: la API gratuita permite unas 200 llamadas al día, y se hace una por año y consulta. Con 3 términos (7 consultas) y 12 años son unas 90. Si añades más términos, reduce el periodo o los tamaños de combinación.

## Cosas a tener en cuenta al usar los datos

- Cada plataforma busca de forma distinta (OpenAlex y S2 en título y resumen; IEEE en todos los metadatos), así que compara **tendencias**, no cifras absolutas entre plataformas.
- El total de Semantic Scholar es aproximado.
- El año en curso siempre está incompleto, y los años recientes siguen subiendo durante meses.
- Para la metodología, cita la fecha de consulta y las cadenas exactas (aparecen al pie de la web y en `historico.csv`).
- Si alguna API falla, el workflow queda en rojo, GitHub te avisa por correo y la web muestra el aviso; los datos que sí se obtuvieron se guardan igualmente.
- Google Scholar no está incluido porque no tiene API y bloquea las consultas automáticas.
