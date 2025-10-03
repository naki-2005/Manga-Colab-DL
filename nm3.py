import os
import shutil
import tempfile
import zipfile
import cloudscraper
import concurrent.futures
import requests
from urllib.parse import urlparse, urljoin, quote_plus
from bs4 import BeautifulSoup
from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn

console = Console()

class MangaClient:
    base_urls = {
        'es': urlparse("https://es.ninemanga.com/"),
        'en': urlparse("https://ninemanga.com/")
    }
    search_param = 'wd'
    query_param = 'waring=1'
    pre_headers = {
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate, br',
        'DNT': '1',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1'
    }

    def __init__(self, language='es'):
        self.language = language
        self.base_url = self.base_urls.get(language, self.base_urls['es'])
        self.search_url = urljoin(self.base_url.geturl(), 'search/')
        self.updates_url = self.base_url.geturl()
        # Usamos cloudscraper en lugar de requests.Session
        self.scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
        self.scraper.headers.update(self.pre_headers)

    def get_url(self, url, retries=3):
        for attempt in range(retries):
            try:
                response = self.scraper.get(url)
                if response.status_code == 404:
                    console.print(f"[red]Error 404: URL no encontrada {url}[/red]")
                    return None
                if response.status_code == 403:
                    console.print(f"[red]Error 403: Acceso denegado en {url}. Cloudflare puede estar bloqueando la solicitud.[/red]")
                    return None
                response.raise_for_status()
                return response.content
            except Exception as e:
                console.print(f"[yellow]Intento {attempt + 1} fallido para {url}: {str(e)}[/yellow]")
                if attempt == retries - 1:
                    return None
                continue

    def mangas_from_page(self, page: bytes):
        if not page:
            return [], [], []
        bs = BeautifulSoup(page, "html.parser")
        container = bs.find("ul", {"class": "direlist"})
        if not container:
            return [], [], []
        cards = container.find_all("li")
        mangas = [card.find_next('a', {'class': 'bookname'}) for card in cards]
        names = [manga.string.strip().title() for manga in mangas if manga and manga.string]
        urls = [manga.get("href") for manga in mangas if manga]
        images = [card.find_next("img").get("src") for card in cards if card.find_next("img")]
        return names, urls, images

    def search(self, query: str = ""):
        query = quote_plus(query)
        request_url = f'{self.search_url}?{self.search_param}={query}'
        content = self.get_url(request_url)
        return self.mangas_from_page(content)

    def chapters_from_page(self, page: bytes):
        if not page:
            return [], []
        bs = BeautifulSoup(page, "html.parser")
        container = bs.find("div", {"class": "chapterbox"})
        if not container:
            return [], []
        lis = container.find_all("li")
        items = [li.find_next('a') for li in lis]
        links = [item.get("href") for item in items if item]
        texts = [item.get("title").strip() for item in items if item and item.get("title")]
        return texts, links

    def get_chapters(self, manga_url: str):
        content = self.get_url(manga_url)
        chapters, links = self.chapters_from_page(content)
        if not chapters:
            content = self.get_url(f'{manga_url}?{self.query_param}')
            chapters, links = self.chapters_from_page(content)
        return chapters, links

    def pictures_from_chapter(self, chapter_url: str):
        images_url = []
        base_chapter = chapter_url.rsplit(".html", 1)[0]
        page = 1
        while True:
            url = f"{base_chapter}-10-{page}.html"
            content = self.get_url(url)
            if content is None:
                break
            bs = BeautifulSoup(content, "html.parser")
            imgs = bs.find_all("img", {"class": "manga_pic"})
            if not imgs:
                break
            new_images = [img.get("src") for img in imgs if img.get("src")]
            if not new_images:
                break
            images_url.extend(new_images)
            page += 1
        return images_url

    def close(self):
        pass

def download_image(url, folder, idx, semaphore):
    headers = {
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'),
        'Referer': 'https://es.ninemanga.com/'
    }
    
    with semaphore:
        scraper = cloudscraper.create_scraper()
        try:
            response = scraper.get(url, headers=headers, stream=True)
            response.raise_for_status()
            
            # Usar chunks para descargar la imagen
            file_path = os.path.join(folder, f'{idx + 1}.jpg')
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            return True
        except Exception as e:
            console.print(f"[red]Error al descargar imagen {url}: {str(e)}[/red]")
            return False

def download_chapter(chapter_url, chapter_name, client, manga_name, drive_path="/content/Drive/MyDrive/Mangas"):
    chapter_name = "".join(c for c in chapter_name if c.isalnum() or c in (' ', '.', '_')).rstrip()
    images = client.pictures_from_chapter(chapter_url)
    if not images:
        console.print(f"[red]Error al descargar el capítulo: {chapter_name} (no se encontraron imágenes)[/red]")
        return None

    folder = tempfile.mkdtemp()
    console.print(f"[bold green]Descargando:[/bold green] {chapter_name}")
    
    # Configurar el semáforo para limitar a 10 hilos concurrentes
    semaphore = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.1f}%"),
        TextColumn("[blue]({task.completed}/{task.total} imágenes)[/blue]"),
        TimeRemainingColumn()
    ) as progress:
        task = progress.add_task("Descargando imágenes...", total=len(images))
        
        # Usar ThreadPoolExecutor para descargas paralelas
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            futures = []
            for idx, img in enumerate(images):
                futures.append(executor.submit(download_image, img, folder, idx, semaphore))
            
            for future in concurrent.futures.as_completed(futures):
                future.result()  # Esto puede lanzar excepciones si ocurrieron durante la descarga
                progress.update(task, advance=1)

    cbz_filename = f'{chapter_name}.cbz'
    try:
        with zipfile.ZipFile(cbz_filename, 'w') as archive:
            for root, _, files in os.walk(folder):
                for file in files:
                    file_path = os.path.join(root, file)
                    archive.write(file_path, arcname=os.path.join(chapter_name, file))
    except Exception as e:
        console.print(f"[red]Error al crear el archivo CBZ: {str(e)}[/red]")
        return None
    finally:
        shutil.rmtree(folder, ignore_errors=True)

    # Mover el archivo a Google Drive
    try:
        # Crear la carpeta del manga en Drive si no existe
        manga_folder = os.path.join(drive_path, manga_name)
        os.makedirs(manga_folder, exist_ok=True)
        
        # Ruta destino en Drive
        drive_cbz_path = os.path.join(manga_folder, cbz_filename)
        
        # Mover el archivo
        shutil.move(cbz_filename, drive_cbz_path)
        console.print(f"[bold green]Archivo movido a:[/bold green] {drive_cbz_path}")
        
        return drive_cbz_path
    except Exception as e:
        console.print(f"[red]Error al mover el archivo a Google Drive: {str(e)}[/red]")
        # Si falla el movimiento, devolver la ruta local
        return cbz_filename

def main():
    # Selección de idioma
    console.print("\n[bold blue]Selecciona el idioma:[/bold blue]")
    console.print("1. Español")
    console.print("2. Inglés")
    
    try:
        lang_choice = console.input("\n[bold blue]Elige el idioma (1-2): [/bold blue]").strip()
        language = 'es' if lang_choice == '1' else 'en'
    except:
        language = 'es'
        console.print("[yellow]Usando español por defecto.[/yellow]")

    client = MangaClient(language=language)
    
    try:
        query = console.input("[bold blue]Introduce el nombre del manga: [/bold blue]").strip()
        if not query:
            console.print("[red]Debes introducir un nombre de manga.[/red]")
            return

        mangas, manga_urls, _ = client.search(query)
        if not mangas:
            console.print("[red]No se encontraron mangas con ese nombre.[/red]")
            return

        console.print("\n[bold underline]Resultados de búsqueda:[/bold underline]")
        for idx, name in enumerate(mangas):
            console.print(f'{idx + 1}. {name}')

        try:
            manga_choice = int(console.input("\n[bold blue]Elige un manga por número: [/bold blue]")) - 1
            if manga_choice < 0 or manga_choice >= len(mangas):
                console.print("[red]Número de manga inválido.[/red]")
                return
        except ValueError:
            console.print("[red]Por favor, introduce un número válido.[/red]")
            return

        selected_manga_name = mangas[manga_choice]
        console.print(f"[bold green]Manga seleccionado:[/bold green] {selected_manga_name}")

        chapters, chapter_urls = client.get_chapters(manga_urls[manga_choice])
        if not chapters:
            console.print("[red]No se encontraron capítulos para este manga.[/red]")
            return

        chapters.reverse()
        chapter_urls.reverse()

        console.print("\n[bold underline]Capítulos disponibles:[/bold underline]")
        for idx, name in enumerate(chapters):
            console.print(f'{idx + 1}. {name}')

        chapter_input = console.input("\n[bold blue]Introduce el rango de capítulos a descargar (ej. 1,3 o solo 5): [/bold blue]").strip()
        chapter_range = chapter_input.split(',')

        try:
            start_chapter = int(chapter_range[0]) - 1
            end_chapter = start_chapter if len(chapter_range) == 1 else int(chapter_range[1]) - 1
            if start_chapter < 0 or end_chapter >= len(chapters) or start_chapter > end_chapter:
                console.print("[red]Rango de capítulos inválido.[/red]")
                return
        except (ValueError, IndexError):
            console.print("[red]Formato de rango inválido. Usa formato como '1,3' o '5'.[/red]")
            return

        console.print(f"\n[bold green]Descargando capítulos del {start_chapter + 1} al {end_chapter + 1}...[/bold green]")
        for idx in range(start_chapter, end_chapter + 1):
            result = download_chapter(chapter_urls[idx], chapters[idx], client, selected_manga_name)
            if result:
                console.print(f"[bold green]Capítulo descargado:[/bold green] {result}")
            else:
                console.print(f"[red]Error al descargar el capítulo:[/red] {chapters[idx]}")

    except KeyboardInterrupt:
        console.print("\n[red]Descarga cancelada por el usuario.[/red]")
    except Exception as e:
        console.print(f"[red]Error inesperado: {str(e)}[/red]")
    finally:
        client.close()

if __name__ == '__main__':
    main()
