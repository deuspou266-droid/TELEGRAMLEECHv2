"""
Manga Leech Command Module
Handles /mangaleech command with source selection, search, and chapter range downloads
"""

import asyncio
import os
import re
import shutil
from functools import partial
from time import time

from pyrogram.filters import regex, user, command
from pyrogram.handlers import CallbackQueryHandler, MessageHandler

from .. import DOWNLOAD_DIR, LOGGER
from ..core.config_manager import Config
from ..helper.ext_utils.bot_utils import new_task
from ..helper.ext_utils.mangaflower_utils import (
    MangaFlowerDownloader,
    parse_chapter_range,
)
from ..helper.ext_utils.status_utils import (
    get_readable_file_size,
    get_progress_bar_string,
)
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import (
    edit_message,
    send_message,
    send_file,
)
from ..helper.telegram_helper.bot_commands import BotCommands
from aiofiles.os import path as aiopath, remove as aio_remove

# Global state to track user interactions
manga_user_state = {}


@new_task
async def mangaleech(client, message):
    """Handle /mangaleech command"""
    user = message.from_user
    user_id = user.id if user else 0
    downloader = MangaFlowerDownloader(logger=LOGGER)

    try:
        # Step 1: Source selection
        buttons = ButtonMaker()
        buttons.data_button("🌸 Flower Mangas", f"manga_flow_{user_id}_source_flower")
        buttons.data_button("❌ Cancelar", f"manga_flow_{user_id}_cancel")

        reply = await send_message(
            message,
            "📚 Escolha a fonte de mangá:",
            buttons.build_menu(1),
        )
        
        # Store state
        manga_user_state[user_id] = {
            "stage": "source_selected",
            "message_id": reply.id,
            "downloader": downloader,
            "last_message": reply,
        }

    except Exception as e:
        LOGGER.error(f"Error in mangaleech: {e}")
        await send_message(message, f"❌ Erro: {str(e)[:200]}")


@new_task
async def manga_source_callback(client, query):
    """Handle source selection callback"""
    user_id = query.from_user.id
    data_parts = query.data.split("_")

    if len(data_parts) < 4:
        await query.answer("❌ Erro ao processar", show_alert=True)
        return

    source = data_parts[-1]
    
    if source == "cancel":
        await query.answer()
        await edit_message(query.message, "❌ Comando cancelado.")
        if user_id in manga_user_state:
            del manga_user_state[user_id]
        return
    
    await query.answer()
    
    # Step 2: Input mode selection  
    buttons = ButtonMaker()
    buttons.data_button("🔗 Link direto", f"manga_flow_{user_id}_mode_link")
    buttons.data_button("🔍 Pesquisar", f"manga_flow_{user_id}_mode_search")
    buttons.data_button("❌ Cancelar", f"manga_flow_{user_id}_cancel")

    await edit_message(
        query.message,
        "🎯 Como você deseja adicionar o mangá?",
        buttons.build_menu(1),
    )
    
    if user_id in manga_user_state:
        manga_user_state[user_id]["stage"] = "mode_selected"
        manga_user_state[user_id]["source"] = source
        manga_user_state[user_id]["last_message"] = query.message


@new_task
async def manga_mode_callback(client, query):
    """Handle input mode selection callback"""
    user_id = query.from_user.id
    data_parts = query.data.split("_")
    
    if len(data_parts) < 4:
        await query.answer("❌ Erro ao processar", show_alert=True)
        return
    
    mode = data_parts[-1]
    
    if mode == "cancel":
        await query.answer()
        await edit_message(query.message, "❌ Comando cancelado.")
        if user_id in manga_user_state:
            del manga_user_state[user_id]
        return
    
    await query.answer()
    
    if mode == "link":
        await edit_message(
            query.message,
            "🔗 Envie o link do mangá:\n(exemplo: https://flowermangas.net/manga/solo-leveling/)",
        )
    elif mode == "search":
        await edit_message(
            query.message,
            "🔍 Envie o nome do mangá que deseja procurar:",
        )
    
    if user_id in manga_user_state:
        manga_user_state[user_id]["stage"] = f"waiting_{mode}"
        manga_user_state[user_id]["mode"] = mode
        manga_user_state[user_id]["last_message"] = query.message


@new_task 
async def manga_message_handler(client, message):
    """Handle user text input for manga (search, link, chapters)"""
    user_id = message.from_user.id
    
    if user_id not in manga_user_state:
        return
    
    state = manga_user_state[user_id]
    stage = state.get("stage", "")
    
    # Route based on current stage
    if stage in ("waiting_search", "waiting_link"):
        await _handle_search_link_input(client, message, user_id, state, stage)
    elif stage == "waiting_chapters":
        await _handle_chapter_input(client, message, user_id, state)
    else:
        return


async def _handle_search_link_input(client, message, user_id, state, stage):
    """Handle search or link input"""
    mode = state.get("mode", "search")
    user_input = message.text.strip()
    downloader = state.get("downloader")
    
    if not downloader:
        await send_message(message, "❌ Erro de configuração.")
        return
    
    try:
        selected_url = None

        if mode == "search":
            # Search for manga
            loading_msg = await send_message(message, "🔍 Pesquisando...")
            results = await downloader.search(user_input)
            
            if not results:
                await edit_message(loading_msg, "❌ Nenhum resultado encontrado.")
                del manga_user_state[user_id]
                return
            
            if len(results) == 1:
                # Auto-select
                selected_url = results[0]["url"]
            else:
                # Show results
                buttons = ButtonMaker()
                for i, result in enumerate(results[:10]):
                    buttons.data_button(f"📖 {result['title'][:35]}", f"manga_flow_{user_id}_result_{i}")
                buttons.data_button("❌ Cancelar", f"manga_flow_{user_id}_cancel")
                
                await edit_message(
                    loading_msg,
                    f"📚 Encontrados {len(results)} resultado(s):",
                    buttons.build_menu(1),
                )
                
                manga_user_state[user_id]["stage"] = "selecting_result"
                manga_user_state[user_id]["search_results"] = results
                manga_user_state[user_id]["last_message"] = loading_msg
                return
            
            manga_user_state[user_id]["selected_url"] = selected_url
            
        elif mode == "link":
            # Validate and normalize link (accept http/https and optional www)
            if re.search(r"https?://(?:www\.)?flowermangas\.net/manga/", user_input):
                normalized = user_input
            elif user_input.startswith("www.flowermangas.net/manga/"):
                normalized = f"https://{user_input}"
            elif user_input.startswith("flowermangas.net/manga/"):
                normalized = f"https://{user_input}"
            else:
                await send_message(message, "❌ Link inválido. Use um link de https://flowermangas.net/manga/")
                return

            selected_url = normalized if normalized.endswith("/") else normalized + "/"
            manga_user_state[user_id]["selected_url"] = selected_url
        
        # Ensure we have a selected URL
        if not selected_url:
            await send_message(message, "❌ Erro: não foi possível determinar o link do mangá. Por favor, tente novamente.")
            if user_id in manga_user_state:
                del manga_user_state[user_id]
            return

        # Get manga info and chapters
        info_msg = await send_message(message, "📊 Carregando informações do mangá...")
        chapters = await downloader.list_chapters(selected_url)
        
        if not chapters:
            await edit_message(info_msg, "❌ Nenhum capítulo encontrado no link.")
            del manga_user_state[user_id]
            return
        
        info = await downloader.get_manga_info(selected_url)
        # Guardar info no estado para usar ao enviar arquivos
        manga_user_state[user_id]["info"] = info
        
        # Build info message
        msg = f"📖 <b>{info.get('title', 'Desconhecido')}</b>\n\n"
        msg += f"📊 <b>Total de capítulos:</b> {len(chapters)}\n"
        
        first_cap = chapters[0].split("capitulo-")[1].rstrip("/")
        last_cap = chapters[-1].split("capitulo-")[1].rstrip("/")
        msg += f"📍 <b>De:</b> Capítulo {first_cap}\n"
        msg += f"📍 <b>Até:</b> Capítulo {last_cap}\n\n"
        
        if info.get("description"):
            desc = info["description"][:200]
            msg += f"📝 {desc}\n\n" if len(desc) == 200 else f"📝 {desc}\n\n"
        
        msg += "📌 <b>Digite o intervalo de capítulos:</b>\n"
        msg += "(ex: 1-5, 10, 15-20)\n\n"
        msg += "<i>ou use 'todos' para baixar todos os capítulos</i>"
        
        await edit_message(info_msg, msg)
        
        manga_user_state[user_id]["stage"] = "waiting_chapters"
        manga_user_state[user_id]["chapters"] = chapters
        manga_user_state[user_id]["last_message"] = info_msg
        
    except Exception as e:
        LOGGER.error(f"Error in _handle_search_link_input: {e}")
        await send_message(message, f"❌ Erro: {str(e)[:200]}")
        if user_id in manga_user_state:
            del manga_user_state[user_id]


@new_task
async def manga_result_callback(client, query):
    """Handle manga search result selection"""
    user_id = query.from_user.id
    data_parts = query.data.split("_")
    
    if len(data_parts) < 5:
        await query.answer("❌ Erro ao processar", show_alert=True)
        return
    
    result_idx = int(data_parts[4])
    
    if user_id not in manga_user_state:
        await query.answer("❌ Sessão expirada", show_alert=True)
        return
    
    state = manga_user_state[user_id]
    results = state.get("search_results", [])
    
    if result_idx >= len(results):
        await query.answer("❌ Resultado inválido", show_alert=True)
        return
    
    await query.answer()
    
    selected_url = results[result_idx]["url"]
    manga_user_state[user_id]["selected_url"] = selected_url
    downloader = state.get("downloader")
    
    try:
        # Get chapters
        info_msg = await send_message(query.message.chat.id, "📊 Carregando capítulos...")
        chapters = await downloader.list_chapters(selected_url)
        info = await downloader.get_manga_info(selected_url)
        # Guardar info no estado para uso posterior
        manga_user_state[user_id]["info"] = info
        
        # Build info message
        msg = f"📖 <b>{info.get('title', 'Desconhecido')}</b>\n\n"
        msg += f"📊 <b>Total de capítulos:</b> {len(chapters)}\n"
        
        first_cap = chapters[0].split("capitulo-")[1].rstrip("/")
        last_cap = chapters[-1].split("capitulo-")[1].rstrip("/")
        msg += f"📍 <b>De:</b> Capítulo {first_cap}\n"
        msg += f"📍 <b>Até:</b> Capítulo {last_cap}\n\n"
        
        if info.get("description"):
            desc = info["description"][:200]
            msg += f"📝 {desc}\n\n" if len(desc) == 200 else f"📝 {desc}\n\n"
        
        msg += "📌 <b>Digite o intervalo de capítulos:</b>\n"
        msg += "(ex: 1-5, 10, 15-20)"
        
        await edit_message(info_msg, msg)
        
        manga_user_state[user_id]["stage"] = "waiting_chapters"
        manga_user_state[user_id]["chapters"] = chapters
        manga_user_state[user_id]["last_message"] = info_msg
        
    except Exception as e:
        LOGGER.error(f"Error in manga_result_callback: {e}")
        await send_message(query.message.chat.id, f"❌ Erro: {str(e)[:200]}")
        if user_id in manga_user_state:
            del manga_user_state[user_id]


async def _handle_chapter_input(client, message, user_id, state):
    """Handle chapter range input"""
    user_input = message.text.strip()
    downloader = state.get("downloader")
    chapters = state.get("chapters", [])
    selected_url = state.get("selected_url")
    
    if not all([downloader, chapters, selected_url]):
        await send_message(message, "❌ Erro de configuração.")
        del manga_user_state[user_id]
        return
    
    try:
        # Parse chapter range
        if user_input.lower() == "todos":
            start, end = 0, float('inf')
        else:
            start, end = parse_chapter_range(user_input)
            if start is None:
                await send_message(message, "❌ Formato inválido. Use: 1-5, 10, 15-20 ou 'todos'")
                return
        
        # Start download with progress
        download_msg = await send_message(message, "⏳ Iniciando download dos capítulos...")
        
        # Filter chapters in range
        selected_chapters = [
            cap for cap in chapters
            if start <= downloader._extract_chapter_number(cap) <= end
        ]
        
        results = []
        for idx, chapter_url in enumerate(selected_chapters, 1):
            # Update progress
            progress_pct = int((idx / len(selected_chapters)) * 100)
            progress_bar = get_progress_bar_string(f"{progress_pct}%")
            chapter_num = chapter_url.split("capitulo-")[1].rstrip("/")
            
            progress_msg = f"⏳ <b>Baixando capítulos...</b>\n\n{progress_bar} {progress_pct}%\n\n"
            progress_msg += f"📥 Capítulo {chapter_num} ({idx}/{len(selected_chapters)})"
            
            await edit_message(download_msg, progress_msg)
            
            cbz_path, page_count = await downloader.download_chapter(
                chapter_url,
                f"{DOWNLOAD_DIR}/manga",
            )
            
            if cbz_path:
                # armazenar também o URL do capítulo para recuperar o número
                results.append((cbz_path, page_count, chapter_url))
        
        if results:
            # Show completion message
            msg = "✅ <b>Download concluído!</b>\n\n"
            total_size = 0
            for filepath, pages, chapter_url in results:
                size = await aiopath.getsize(filepath)
                total_size += size
                chapter_name = filepath.split("/")[-1].replace(".cbz", "")
                msg += f"📦 {chapter_name}\n   {pages} páginas | {get_readable_file_size(size)}\n"
            
            msg += f"\n<b>Total:</b> {len(results)} capítulos | {get_readable_file_size(total_size)}"
            await edit_message(download_msg, msg)
            
            # Send files to Telegram
            send_msg = await send_message(message, "📤 Enviando capítulos para Telegram...")
            
            for idx, (filepath, pages, chapter_url) in enumerate(results, 1):
                try:
                    chapter_name = filepath.split("/")[-1]
                    # Update sending progress
                    sending_pct = int((idx / len(results)) * 100)
                    sending_bar = get_progress_bar_string(f"{sending_pct}%")
                    
                    # Recuperar número do capítulo a partir do URL
                    try:
                        chapter_num = chapter_url.split("capitulo-")[1].rstrip("/")
                    except Exception:
                        chapter_num = chapter_name.replace('.cbz','')

                    obra_title = state.get("info", {}).get("title", chapter_name.replace('.cbz',''))
                    display_title = f"{obra_title} cap {chapter_num}"

                    sending_info = f"📤 Enviando: {sending_bar} {sending_pct}%\n{idx}/{len(results)} - {display_title}"
                    await edit_message(send_msg, sending_info)

                    # Send the file with formatted caption
                    caption = f"{display_title}\n📄 {pages} páginas"
                    await send_file(message, filepath, caption=caption)
                    
                    # Delete file after successful send
                    try:
                        await aio_remove(filepath)
                        LOGGER.info(f"Deleted: {filepath}")
                    except Exception as del_e:
                        LOGGER.warning(f"Could not delete {filepath}: {del_e}")
                    
                except Exception as e:
                    LOGGER.error(f"Error sending file {filepath}: {e}")
                    await send_message(message, f"⚠️ Erro ao enviar {chapter_name}: {str(e)[:100]}")
            
            # Final message
            await edit_message(send_msg, "✅ <b>Todos os capítulos foram enviados!</b>")
            
            # Cleanup destination folder
            manga_dir = f"{DOWNLOAD_DIR}/manga"
            try:
                if await aiopath.exists(manga_dir):
                    # Remove remaining files
                    for root, dirs, files in os.walk(manga_dir, topdown=False):
                        for file in files:
                            try:
                                await aio_remove(os.path.join(root, file))
                            except:
                                pass
                    # Remove empty directories
                    try:
                        shutil.rmtree(manga_dir)
                        LOGGER.info(f"Cleaned up manga directory: {manga_dir}")
                    except:
                        pass
            except Exception as cleanup_e:
                LOGGER.warning(f"Could not cleanup {manga_dir}: {cleanup_e}")
        else:
            await edit_message(download_msg, "❌ Erro ao baixar capítulos.")
        
        # Clean up state
        del manga_user_state[user_id]
        
    except Exception as e:
        LOGGER.error(f"Error in _handle_chapter_input: {e}")
        await send_message(message, f"❌ Erro: {str(e)[:200]}")
        if user_id in manga_user_state:
            del manga_user_state[user_id]
