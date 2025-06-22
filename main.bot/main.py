import os
import re
import io
import zipfile
import tempfile
import base64
import logging
import warnings
import asyncio

from telethon import TelegramClient, events, Button
from ebooklib import epub
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from PIL import Image
from lxml import etree
from docx import Document

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
logging.basicConfig(level=logging.INFO)

# Очистка временной директории при запуске
def clean_temp_dir():
    tmpdir = tempfile.gettempdir()
    for root, dirs, files in os.walk(tmpdir):
        for name in files:
            try:
                os.remove(os.path.join(root, name))
            except:
                pass
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except:
                pass

clean_temp_dir()

# Конфигурация Telegram
api_id = 24519852
api_hash = '2186f59fdf9c2ad4e7ddf0deb250ff0c'
bot_token = os.environ.get("BOT_TOKEN")

if not bot_token:
    raise RuntimeError("BOT_TOKEN не установлен!")

# 👇 Основной асинхронный запуск
async def main():
    client = TelegramClient("bot", api_id, api_hash)
    await client.start(bot_token=bot_token)

    @client.on(events.NewMessage(pattern='/start'))
    async def handler(event):
        await event.respond("✅ Бот запущен и работает!")

    print("✅ Бот успешно запущен.")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
    

RESOLUTIONS = {
    'Удалить изображения': None,
    '64p': (64, 64),
    '144p': (256, 144),
    '360p': (640, 360),
    '480p': (854, 480),
    '720p': (1280, 720),
    '1080p': (1920, 1080)
}

user_files = {}
user_mode = {}
CHAPTER_RE = re.compile(r"(Глава\s*\d+)[^\n<]*", re.IGNORECASE)

# Команды для установки режимов
@client.on(events.NewMessage(pattern=r'/compress'))
async def set_compress_mode(event):
    user_id = event.sender_id
    user_mode[user_id] = 'compress'
    await event.respond("Режим сжатия активирован. Отправьте файл для обработки.")

@client.on(events.NewMessage(pattern=r'/convert'))
async def set_convert_mode(event):
    user_id = event.sender_id
    user_mode[user_id] = 'convert'
    await event.respond("Режим конвертации активирован. Отправьте файл для обработки.")

@client.on(events.NewMessage(pattern=r'/extract'))
async def set_extract_mode(event):
    user_id = event.sender_id
    user_mode[user_id] = 'extract'
    await event.respond("Режим извлечения глав активирован. Отправьте EPUB файл для обработки.")

@client.on(events.NewMessage(pattern=r'/start'))
async def start_handler(event):
    await event.respond("""
Добро пожаловать! Доступные команды:
/compress - Сжатие изображений в файлах
/convert - Конвертация между форматами
/extract - Извлечение глав из EPUB
/help - Помощь
    """)

@client.on(events.NewMessage(pattern=r'/help'))
async def help_handler(event):
    await event.respond("""
Команды бота:
/compress - Сжатие изображений в FB2, DOCX, EPUB
/convert - Конвертация между форматами (EPUB, FB2, DOCX, TXT)
/extract - Создание оглавления для EPUB

Поддерживаемые форматы: .epub, .fb2, .docx, .txt
    """)

# Приём файлов
@client.on(events.NewMessage(incoming=True))
async def handle_file(event):
    if not event.file:
        return
    
    user_id = event.sender_id
    mode = user_mode.get(user_id)
    if not mode:
        return

    filename = event.file.name or 'file'
    ext = os.path.splitext(filename)[1].lower()

    try:
        file_data = io.BytesIO()
        await client.download_media(event.message, file=file_data)
        file_data.seek(0)

        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(file_data.read())
            tmp_path = tmp.name

        user_files[user_id] = (filename, tmp_path)

        if mode == 'compress' and ext in ['.epub', '.fb2', '.docx']:
            buttons = [[Button.inline(label, data=label.encode())] for label in RESOLUTIONS]
            await event.respond("Выберите способ обработки изображений:", buttons=buttons)

        elif mode == 'convert' and ext in ['.epub', '.fb2', '.docx', '.txt']:
            buttons = [
                [Button.inline("В DOCX", b"to_docx"), Button.inline("В FB2", b"to_fb2")],
                [Button.inline("В EPUB", b"to_epub"), Button.inline("В TXT", b"to_txt")]
            ]
            await event.respond("Выберите формат для конвертации:", buttons=buttons)

        elif mode == 'extract' and ext == '.epub':
            await event.respond("Файл получен. Начинаю обработку...")
            try:
                chapters, images = extract_chapters_from_epub(tmp_path)
                if not chapters:
                    await event.respond("Главы не найдены.")
                    return
                    
                base = os.path.splitext(filename)[0]
                output_path = os.path.join(tempfile.gettempdir(), f"{base}_converted.epub")
                build_epub(base, chapters, images, output_path)
                await client.send_file(user_id, output_path, caption="EPUB пересобран с оглавлением.")
                os.remove(output_path)
            except Exception as e:
                await event.respond(f"Ошибка: {e}")
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                user_mode.pop(user_id, None)
                user_files.pop(user_id, None)
        else:
            await event.respond(f"Неподдерживаемый формат для режима {mode}.")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    except Exception as e:
        await event.respond(f"Ошибка при обработке файла: {e}")
        user_mode.pop(user_id, None)
        user_files.pop(user_id, None)

# Inline-кнопки
@client.on(events.CallbackQuery)
async def handle_button(event):
    user_id = event.sender_id
    mode = user_mode.get(user_id)
    if not mode:
        await event.answer("Сессия истекла. Начните заново.", alert=True)
        return

    data = event.data.decode()
    file_info = user_files.get(user_id)
    if not file_info:
        await event.answer("Файл не найден. Начните заново.", alert=True)
        return
        
    filename, filepath = file_info
    if not os.path.exists(filepath):
        await event.answer("Файл не найден. Начните заново.", alert=True)
        return

    ext = os.path.splitext(filename)[1].lower()

    try:
        if mode == 'compress':
            resolution = RESOLUTIONS.get(data)
            await event.edit(f"Обработка файла {filename}...")
            
            if ext == '.fb2':
                await process_fb2(event, user_id, filename, filepath, resolution)
            elif ext == '.docx':
                await process_docx(event, user_id, filename, filepath, resolution)
            elif ext == '.epub':
                await process_epub_compression(event, user_id, filename, filepath, resolution)

        elif mode == 'convert':
            target_ext = data.replace("to_", ".")
            if ext == target_ext:
                await event.edit("Файл уже в этом формате.")
                await client.send_file(user_id, filepath, caption=filename)
            else:
                await event.edit(f"Конвертация {filename}...")
                new_filename = os.path.splitext(filename)[0] + target_ext
                new_path = os.path.join(tempfile.gettempdir(), new_filename)
                
                # Простая конвертация (копирование с изменением расширения)
                with open(filepath, 'rb') as src, open(new_path, 'wb') as dst:
                    dst.write(src.read())
                    
                await client.send_file(user_id, new_path, caption=f"Конвертация завершена: {new_filename}")
                os.remove(new_path)

    except Exception as e:
        await event.edit(f"Ошибка обработки: {e}")
    finally:
        # Очистка
        if os.path.exists(filepath):
            os.remove(filepath)
        user_files.pop(user_id, None)
        user_mode.pop(user_id, None)

# Обработка изображений FB2
async def process_fb2(event, user_id, filename, filepath, resolution):
    try:
        ns = {'fb2': 'http://www.gribuser.ru/xml/fictionbook/2.0'}
        tree = etree.parse(filepath)
        root = tree.getroot()
        binaries = root.xpath('//fb2:binary', namespaces=ns)

        changed = deleted = 0
        image_binaries = [b for b in binaries if 'image' in (b.get('content-type') or '')]
        total = len(image_binaries)
        current = 0
        
        if total == 0:
            await event.edit("В файле нет изображений для обработки.")
            return

        for binary in image_binaries:
            current += 1
            try:
                await event.edit(f"Обработка изображения {current} из {total}...")
                
                if resolution is None:
                    root.remove(binary)
                    deleted += 1
                else:
                    img_data = base64.b64decode(binary.text)
                    img = Image.open(io.BytesIO(img_data)).convert('RGB')
                    img.thumbnail(resolution, Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format='JPEG', quality=30)
                    binary.text = base64.b64encode(buf.getvalue()).decode()
                    binary.set('content-type', 'image/jpeg')
                    changed += 1
            except Exception as e:
                logging.error(f"Ошибка обработки изображения: {e}")
                continue

        base, ext = os.path.splitext(filename)
        out_path = os.path.join(tempfile.gettempdir(), f"{base}_compressed{ext}")
        tree.write(out_path, encoding='utf-8', xml_declaration=True)
        
        await client.send_file(user_id, out_path, caption=f"FB2 обработан: сжато {changed}, удалено {deleted}")
        os.remove(out_path)
        
    except Exception as e:
        await event.edit(f"Ошибка обработки FB2: {e}")

# DOCX
async def process_docx(event, user_id, filename, filepath, resolution):
    try:
        doc = Document(filepath)
        changed = deleted = 0
        total = len(doc.inline_shapes)
        current = 0

        if total == 0:
            await event.edit("В документе нет изображений для обработки.")
            return

        if resolution is None:
            # Удаление изображений
            shapes_to_remove = []
            for shape in doc.inline_shapes:
                shapes_to_remove.append(shape)
                
            for shape in shapes_to_remove:
                current += 1
                await event.edit(f"Удаление изображения {current} из {total}...")
                try:
                    shape._element.getparent().remove(shape._element)
                    deleted += 1
                except Exception as e:
                    logging.error(f"Ошибка удаления изображения: {e}")
                    continue
        else:
            # Сжатие изображений
            for shape in doc.inline_shapes:
                current += 1
                await event.edit(f"Сжатие изображения {current} из {total}...")
                try:
                    r_id = shape._inline.graphic.graphicData.pic.blipFill.blip.embed
                    img_part = doc.part.related_parts[r_id]
                    img = Image.open(io.BytesIO(img_part.blob)).convert('RGB')
                    img.thumbnail(resolution, Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format='JPEG', quality=30)
                    img_part._blob = buf.getvalue()
                    changed += 1
                except Exception as e:
                    logging.error(f"Ошибка сжатия изображения: {e}")
                    continue

        base, ext = os.path.splitext(filename)
        out_path = os.path.join(tempfile.gettempdir(), f"{base}_compressed{ext}")
        doc.save(out_path)
        
        await client.send_file(user_id, out_path, caption=f"DOCX обработан: сжато {changed}, удалено {deleted}")
        os.remove(out_path)
        
    except Exception as e:
        await event.edit(f"Ошибка обработки DOCX: {e}")

# EPUB
async def process_epub_compression(event, user_id, filename, filepath, resolution):
    try:
        book = epub.read_epub(filepath)
        changed = deleted = 0
        images = [item for item in list(book.get_items()) if item.media_type and item.media_type.startswith("image/")]
        total = len(images)
        current = 0

        if total == 0:
            await event.edit("В EPUB нет изображений для обработки.")
            return

        items_to_remove = []
        
        for item in images:
            current += 1
            await event.edit(f"Обработка изображения {current} из {total}...")

            if resolution is None:
                items_to_remove.append(item)
                deleted += 1
            else:
                try:
                    img = Image.open(io.BytesIO(item.content)).convert("RGB")
                    img.thumbnail(resolution, Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=30)
                    item.content = buf.getvalue()
                    item.media_type = "image/jpeg"
                    changed += 1
                except Exception as e:
                    logging.error(f"Ошибка обработки изображения: {e}")
                    continue

        # Удаляем изображения после итерации
        for item in items_to_remove:
            book.items.remove(item)

        base, ext = os.path.splitext(filename)
        out_path = os.path.join(tempfile.gettempdir(), f"{base}_compressed{ext}")
        epub.write_epub(out_path, book)
        
        await client.send_file(user_id, out_path, caption=f"EPUB обработан: сжато {changed}, удалено {deleted}")
        os.remove(out_path)
        
    except Exception as e:
        await event.edit(f"Ошибка обработки EPUB: {e}")

# EPUB: главы
def extract_chapters_from_epub(epub_path):
    temp_dir = tempfile.mkdtemp()
    html_blocks = []
    images = {}

    try:
        with zipfile.ZipFile(epub_path, 'r') as zf:
            zf.extractall(temp_dir)

        for root, _, files in os.walk(temp_dir):
            for file in files:
                path = os.path.join(root, file)
                if file.lower().endswith((".xhtml", ".html", ".htm")):
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            html_blocks.append(BeautifulSoup(f, "lxml"))
                    except Exception as e:
                        logging.error(f"Ошибка чтения HTML файла {file}: {e}")
                        continue
                elif file.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp")):
                    images[file] = path

        chapters = []
        title, content, num = None, "", 0
        
        for soup in html_blocks:
            if not soup.body:
                continue
                
            for elem in soup.body.find_all(recursive=False):
                text = elem.get_text(strip=True)
                match = CHAPTER_RE.match(text or "")
                if match:
                    if title:
                        chapters.append((num, title, content.strip()))
                    title = match.group(1)
                    num_match = re.search(r'\d+', title)
                    num = int(num_match.group()) if num_match else 0
                    content = f"<h1>{title}</h1>"
                else:
                    content += str(elem)
                    
        if title:
            chapters.append((num, title, content.strip()))

        # Удаляем дубликаты
        seen, result = set(), []
        for n, t, c in sorted(chapters, key=lambda x: x[0]):
            if t not in seen:
                result.append((n, t, c))
                seen.add(t)
                
        return result, images
        
    except Exception as e:
        logging.error(f"Ошибка извлечения глав: {e}")
        return [], {}
    finally:
        # Очистка временной директории
        try:
            import shutil
            shutil.rmtree(temp_dir)
        except:
            pass

def build_epub(title, chapters, image_paths, output_path):
    try:
        book = epub.EpubBook()
        book.set_identifier("converted")
        book.set_title(title)
        book.set_language("ru")
        book.add_author("Chronos Bot")

        spine = ['nav']
        toc = []

        # Добавляем изображения
        for fname, path in image_paths.items():
            try:
                ext = os.path.splitext(fname)[1][1:].lower()
                mime = f"image/{'jpeg' if ext in ['jpg', 'jpeg'] else ext}"
                with open(path, 'rb') as f:
                    book.add_item(epub.EpubItem(
                        uid=fname,
                        file_name=f"images/{fname}",
                        media_type=mime,
                        content=f.read()
                    ))
            except Exception as e:
                logging.error(f"Ошибка добавления изображения {fname}: {e}")

        # Добавляем главы
        for i, (num, title, html_body) in enumerate(chapters, 1):
            try:
                html = epub.EpubHtml(title=title, file_name=f"chap_{i}.xhtml", lang='ru')
                html.content = html_body
                book.add_item(html)
                spine.append(html)
                toc.append(epub.Link(html.file_name, title, f"chap_{i}"))
            except Exception as e:
                logging.error(f"Ошибка добавления главы {title}: {e}")

        book.spine = spine
        book.toc = toc
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())

        epub.write_epub(output_path, book)
        
    except Exception as e:
        logging.error(f"Ошибка создания EPUB: {e}")
        raise

# Запуск
async def main():
    try:
        await client.start(bot_token=bot_token)
        print("Бот запущен.")
        await client.run_until_disconnected()
    except Exception as e:
        logging.error(f"Ошибка запуска бота: {e}")

if __name__ == "__main__":
    asyncio.run(main())
