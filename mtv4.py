import os
import requests
import shutil
import tempfile
import zipfile
import subprocess
import json
import time
from urllib.parse import urlparse, urljoin, quote_plus
from bs4 import BeautifulSoup
import re
from rich.console import Console
from rich.progress import Progress, BarColumn, TimeRemainingColumn, TextColumn
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

console = Console()

def shorten_filename(filename):
    if len(filename) > 50:
        return filename[:30] + '...' + filename[-20:]
    return filename
    
class MangaClient:
    base_url = urlparse("https://www.mangatv.net/")
    search_param = 's'
    pre_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:97.0) Gecko/20100101 Firefox/97.0',
        'Accept-Language': 'es-ES,es;q=0.9'
    }

    def __init__(self):
        self.search_url = urljoin(self.base_url.geturl(), 'lista')
        self.session = requests.Session()
        self.session.headers.update(self.pre_headers)
        # Configuración mejorada de reintentos con backoff exponencial
        retries = Retry(
            total=10,
            backoff_factor=2,  # Aumentamos el factor de backoff
            status_forcelist=[408, 429, 500, 502, 503, 504],
            allowed_methods=frozenset(['GET', 'POST'])
        )
        adapter = HTTPAdapter(
            max_retries=retries,
            pool_connections=20,
            pool_maxsize=100,
            pool_block=True
        )
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)

    def get_url(self, url, max_retries=3, timeout=30):
        retries = 0
        last_exception = None
        
        while retries < max_retries:
            try:
                response = self.session.get(url, timeout=timeout)
                response.raise_for_status()
                return response.content
            except requests.exceptions.RequestException as e:
                last_exception = e
                retries += 1
                wait_time = 2 ** retries  # Espera exponencial
                console.print(f"[yellow]Intento {retries}/{max_retries} fallido para {url}. Reintentando en {wait_time} segundos...[/yellow]")
                time.sleep(wait_time)
        
        console.print(f"[red]Error después de {max_retries} intentos para {url}: {last_exception}[/red]")
        raise last_exception

    def mangas_from_page(self, page: bytes):
        bs = BeautifulSoup(page, "html.parser")
        container = bs.find_all("div", {"class": "bsx"})
        mangas = [card.find_next('a') for card in container]
        names = [manga.get("title").strip().title() for manga in mangas]
        urls = [manga.get("href") for manga in mangas]
        images = [card.find_next("img").get("src") for card in container]
        return names, urls, images

    def search(self, query: str = ""):
        query = quote_plus(query)
        request_url = f'{self.search_url}?{self.search_param}={query}'
        content = self.get_url(request_url)
        return self.mangas_from_page(content)

    def chapters_from_page(self, page: bytes):
        bs = BeautifulSoup(page, "html.parser")
        container = bs.find("div", {"id": "chapterlist"})
        if not container:
            return [], []
        items = container.find_all("li")
        links = [item.find("a", {"class": "dload"}).get("href") for item in items]
        texts = [item.find("span", {"class": "chapternum"}).text.strip() for item in items]

        # Filtrar capítulos duplicados por nombre
        unique_chapters = {}
        for text, link in zip(texts, links):
            if text not in unique_chapters:
                unique_chapters[text] = link
        return list(unique_chapters.keys()), list(unique_chapters.values())

    def get_chapters(self, manga_url: str):
        content = self.get_url(manga_url)
        chapters, links = self.chapters_from_page(content)
        return chapters, links

    def pictures_from_chapter(self, chapter_url: str):
        """
        • Descarga el contenido de la página del capítulo con reintentos.
        • Maneja mejor los errores en la ejecución de Node.js.
        • Añade más logs para diagnóstico de problemas.
        """
        try:
            content = self.get_url(chapter_url, max_retries=5, timeout=60)
            if content is None:
                console.print("[red]Error: No se pudo obtener el contenido del capítulo.[/red]")
                return []
            
            html = content.decode('utf-8', errors='ignore')
            soup = BeautifulSoup(html, "html.parser")
            script_tag = soup.find("script", string=re.compile(r"eval\(function\(p,a,c,k,e,d\)"))
            
            if not script_tag:
                console.print("[red]Error: No se encontró el script ofuscado con las URLs de las imágenes.[/red]")
                return []
                
            packed_script = script_tag.string

            # Generar el script temporal para Node.js con más manejo de errores
            node_script = f"""
global.atob = function(s) {{
    return Buffer.from(s, 'base64').toString('utf8');
}};

global.ts_reader = {{
    run: function(data) {{
        try {{
            console.log(JSON.stringify(data));
        }} catch (e) {{
            console.error("ERROR_STRINGIFY:" + e);
        }}
        process.exit(0);
    }}
}};

try {{
    {packed_script}
}} catch (e) {{
    console.error("ERROR_EVAL:" + e);
    process.exit(1);
}}
"""
            try:
                with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8") as temp_js:
                    temp_js.write(node_script)
                    temp_js.flush()
                    temp_js_name = temp_js.name
            except Exception as e:
                console.print(f"[red]Error al escribir el archivo temporal en Node.js: {e}[/red]")
                return []

            try:
                # Aumentamos el timeout para conexiones lentas
                proc = subprocess.run(
                    ["node", temp_js_name],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=60,  # Mayor tiempo de espera
                    text=True,
                    encoding='utf-8'
                )
                os.unlink(temp_js_name)
                
                if proc.returncode != 0:
                    error_msg = proc.stderr.strip()
                    if "ERROR_EVAL:" in error_msg:
                        console.print(f"[red]Error en la evaluación del script: {error_msg.split('ERROR_EVAL:')[-1]}[/red]")
                    else:
                        console.print(f"[red]Error al ejecutar Node.js: {error_msg}[/red]")
                    return []
                    
                output = proc.stdout.strip()
            except subprocess.TimeoutExpired:
                os.unlink(temp_js_name)
                console.print("[red]Error: Tiempo de espera agotado al ejecutar Node.js[/red]")
                return []
            except Exception as e:
                os.unlink(temp_js_name)
                console.print(f"[red]Error al ejecutar Node.js: {e}[/red]")
                return []

            try:
                data = json.loads(output)
            except json.JSONDecodeError as e:
                console.print(f"[red]Error al transformar la salida de Node.js a JSON: {e}[/red]")
                console.print(f"[yellow]Salida recibida:[/yellow] {output[:200]}...")  # Log parcial para diagnóstico
                return []

            # Primero se intenta extraer la propiedad 'n' o 'V'
            base_url_raw = data.get('n') or data.get('V')
            if base_url_raw:
                try:
                    tokens = data['4'][0]['3']
                except Exception as e:
                    console.print(f"[red]Error accediendo a los tokens de imágenes: {e}[/red]")
                    return []
                    
                if not tokens:
                    console.print("[red]Error: La lista de tokens para imágenes está vacía.[/red]")
                    return []
                    
                # Simular que se elimina "==" si están presentes
                decoded_tokens = [token[:-2] if token.endswith("==") else token for token in tokens]
                base_url_processed = base_url_raw.replace("8://", "https://").replace("7.6", "mangatv.net")
                final_images = []
                
                for token in decoded_tokens:
                    url = base_url_processed.replace("k", token)
                    if url.endswith(".j"):
                        url += "pg"
                    final_images.append(url)
                    
                return final_images
                
            elif "sources" in data and data["sources"]:
                # Si no se encontró 'n' o 'V', usar directamente "sources"
                sources = data["sources"]
                images = sources[0].get("images", [])
                if not images:
                    console.print("[red]Error: La lista de imágenes en 'sources' está vacía.[/red]")
                    return []
                    
                final_images = []
                for img in images:
                    if img.startswith("//"):
                        final_images.append("https:" + img)
                    elif img.startswith("http"):
                        final_images.append(img)
                    else:
                        final_images.append("https://" + img)
                return final_images
                
            else:
                console.print("[red]Error: No se encontró la propiedad 'n', 'V' ni 'sources' en los datos.[/red]")
                console.print(f"[yellow]Datos retornados:[/yellow] {json.dumps(data, indent=2)[:500]}...")  # Log parcial
                return []
                
        except Exception as e:
            console.print(f"[red]Error inesperado en pictures_from_chapter: {e}[/red]")
            return []

    def close(self):
        self.session.close()

def download_image(url, folder, idx, task, progress, session):
    max_retries = 5
    retry_delay = 2  # segundos
    
    for attempt in range(max_retries):
        try:
            response = session.get(url, stream=True, timeout=(10, 30))  # 10s conexión, 30s lectura
            response.raise_for_status()
            
            file_path = os.path.join(folder, f'{idx + 1:04d}.jpg')
            temp_file_path = f"{file_path}.tmp"
            
            # Descarga en bloques con manejo de errores
            with open(temp_file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:  # Filtrar keep-alive chunks
                        f.write(chunk)
            
            # Renombrar solo si la descarga fue exitosa
            os.rename(temp_file_path, file_path)
            progress.update(task, advance=1)
            return
            
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                console.print(f"[yellow]Intento {attempt + 1}/{max_retries} fallido para imagen {idx + 1}. Reintentando en {retry_delay} segundos...[/yellow]")
                time.sleep(retry_delay)
                retry_delay *= 2  # Backoff exponencial
            else:
                console.print(f"[red]Error persistente al descargar la imagen {idx + 1}: {e}[/red]")
                progress.update(task, advance=1)
                return
        except Exception as e:
            console.print(f"[red]Error inesperado al descargar imagen {idx + 1}: {e}[/red]")
            progress.update(task, advance=1)
            return

def download_chapter(chapter_url, manga_name, chapter_name, client):
    images = client.pictures_from_chapter(chapter_url)
    if not images:
        console.print(f"[red]Error al obtener las imágenes del capítulo:[/red] {chapter_name}")
        return False

    folder = os.path.join(tempfile.gettempdir(), f"{manga_name} - {chapter_name}")
    os.makedirs(folder, exist_ok=True)
    
    console.print(f"[green]Descargando {manga_name} - {chapter_name} ({len(images)} imágenes)[/green]")
    
    # Configurar sesión con reintentos
    session = requests.Session()
    retries = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[408, 429, 500, 502, 503, 504]
    )
    session.mount('http://', HTTPAdapter(max_retries=retries))
    session.mount('https://', HTTPAdapter(max_retries=retries))
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:97.0) Gecko/20100101 Firefox/97.0'
    })

    try:
        with Progress(
            "[progress.percentage]{task.percentage:>3.1f}%",
            BarColumn(),
            TextColumn("{task.completed} de {task.total} imágenes"),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("[cyan]Descargando imágenes...", total=len(images))
            
            # Reducir workers para conexiones lentas
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [
                    executor.submit(download_image, img, folder, idx, task, progress, session)
                    for idx, img in enumerate(images)
                ]
                for future in as_completed(futures):
                    future.result()  # Para capturar excepciones si las hay
        
        # Verificar que todas las imágenes se descargaron
        downloaded_images = len([name for name in os.listdir(folder) if name.endswith('.jpg')])
        if downloaded_images != len(images):
            console.print(f"[yellow]Advertencia: Solo se descargaron {downloaded_images} de {len(images)} imágenes[/yellow]")
        
        cbz_filename = f'{manga_name} - {chapter_name}.cbz'
        cbz_filename = shorten_filename(cbz_filename)
        temp_cbz = f"{cbz_filename}.tmp"
        temp_cbz = shorten_filename(temp_cbz)

        try:
            with zipfile.ZipFile(temp_cbz, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for root, _, files in os.walk(folder):
                    for file in sorted(files):
                        if file.endswith('.jpg'):
                            archive.write(
                                os.path.join(root, file),
                                arcname=os.path.join(chapter_name, file)
                            )
            
            # Renombrar solo si el ZIP se creó correctamente
            os.rename(temp_cbz, cbz_filename)
            console.print(f"[blue]Capítulo descargado y empaquetado:[/blue] {cbz_filename}")
            return True
            
        except Exception as e:
            console.print(f"[red]Error al crear el archivo CBZ: {e}[/red]")
            if os.path.exists(temp_cbz):
                os.remove(temp_cbz)
            return False
            
    finally:
        session.close()
        try:
            shutil.rmtree(folder)
        except Exception as e:
            console.print(f"[yellow]Advertencia al eliminar la carpeta temporal: {e}[/yellow]")

def main():
    client = MangaClient()
    try:
        while True:
            try:
                query = input("Introduce el nombre del manga: ").strip()
                if not query:
                    console.print("[red]Error: El nombre del manga no puede estar vacío[/red]")
                    continue
                    
                mangas, manga_urls, _ = client.search(query)
                if not mangas:
                    console.print("[red]No se encontraron resultados. Por favor, intenta con otro nombre de manga.[/red]")
                    continue

                console.print("\n[bold]Resultados encontrados:[/bold]")
                for idx, name in enumerate(mangas):
                    console.print(f'{idx + 1}. {name}')

                try:
                    manga_choice = int(input("\nElige un manga por número: ")) - 1
                    if manga_choice < 0 or manga_choice >= len(mangas):
                        console.print("[red]Error: Número de manga inválido[/red]")
                        continue
                except ValueError:
                    console.print("[red]Error: Por favor ingresa un número válido[/red]")
                    continue

                console.print(f"\n[bold]Obteniendo capítulos para {mangas[manga_choice]}...[/bold]")
                chapters, chapter_urls = client.get_chapters(manga_urls[manga_choice])
                manga_name = mangas[manga_choice]

                if not chapters:
                    console.print("[red]No se encontraron capítulos para este manga[/red]")
                    continue

                chapters.reverse()
                chapter_urls.reverse()

                console.print("\n[bold]Capítulos disponibles:[/bold]")
                for idx, name in enumerate(chapters):
                    console.print(f'{idx + 1}. {name}')

                while True:
                    chapter_range = input("\nIntroduce el rango de capítulos a descargar (e.g., 1,3 o '1' para un solo capítulo): ").strip()
                    try:
                        if ',' in chapter_range:
                            start, end = map(int, chapter_range.split(','))
                            start_chapter = start - 1
                            end_chapter = end - 1
                        else:
                            start_chapter = end_chapter = int(chapter_range) - 1

                        if start_chapter < 0 or end_chapter >= len(chapters) or start_chapter > end_chapter:
                            console.print("[red]Error: Rango de capítulos inválido[/red]")
                            continue
                        break
                    except ValueError:
                        console.print("[red]Error: Formato inválido. Usa '1,3' o '1'[/red]")

                console.print(f"\n[bold]Preparando para descargar capítulos {start_chapter + 1} a {end_chapter + 1}...[/bold]")
                
                # Descargar capítulos en serie para conexiones lentas
                for idx in range(start_chapter, end_chapter + 1):
                    success = download_chapter(
                        chapter_urls[idx], 
                        manga_name, 
                        chapters[idx], 
                        client
                    )
                    if not success:
                        console.print(f"[red]Se detuvo la descarga debido a errores[/red]")
                        break
                        
                break
                    
            except KeyboardInterrupt:
                console.print("\n[yellow]Operación cancelada por el usuario[/yellow]")
                break
            except Exception as e:
                console.print(f"[red]Error inesperado: {e}[/red]")
                continue
                
    finally:
        client.close()

if __name__ == '__main__':
    main()
