from logging import getLogger
from os import path as ospath, makedirs, getcwd, chdir, path
from shutil import rmtree, copyfile
from secrets import token_hex
from contextlib import suppress
from shutil import rmtree

from spotdl import Spotdl
from spotdl.types.song import Song
from threading import Lock
from time import sleep as _sleep

from .... import task_dict_lock, task_dict
from ....core.config_manager import BinConfig, Config
from ...ext_utils.bot_utils import sync_to_async, async_to_sync
from ...ext_utils.task_manager import (
    check_running_tasks,
    stop_duplicate_check,
    limit_checker,
)
from ...mirror_leech_utils.status_utils.queue_status import QueueStatus
from ...telegram_helper.message_utils import send_status_message
from ..status_utils.spotdl_status import SpotdlStatus

LOGGER = getLogger(__name__)

# Module-level singleton Spotdl client to avoid repeated initialization errors
_GLOBAL_SPOTDL_CLIENT = None
_GLOBAL_SPOTDL_LOCK = Lock()


def get_spotdl_client(ffmpeg=None, bitrate="320k", fmt="mp3", threads=4):
    global _GLOBAL_SPOTDL_CLIENT
    if _GLOBAL_SPOTDL_CLIENT is not None:
        return _GLOBAL_SPOTDL_CLIENT

    # Ensure only one thread tries to initialize at a time
    with _GLOBAL_SPOTDL_LOCK:
        if _GLOBAL_SPOTDL_CLIENT is not None:
            return _GLOBAL_SPOTDL_CLIENT

        # Get Spotify credentials from config if available
        client_id = Config.SPOTIFY_CLIENT_ID or None
        client_secret = Config.SPOTIFY_CLIENT_SECRET or None

        # Try a couple times in case of race conditions inside external lib
        for attempt in range(3):
            try:
                client = Spotdl(
                    client_id=client_id,
                    client_secret=client_secret,
                    headless=True,
                    downloader_settings={
                        "ffmpeg": ffmpeg or get_ffmpeg_path(),
                        "bitrate": bitrate,
                        "format": fmt,
                        "threads": threads,
                    },
                )
                _GLOBAL_SPOTDL_CLIENT = client
                LOGGER.info("Spotdl client initialized (singleton)")
                return _GLOBAL_SPOTDL_CLIENT
            except Exception as e:
                # If error indicates client already initialized, wait and retry
                msg = str(e).lower()
                LOGGER.warning(f"Spotdl init attempt {attempt+1} failed: {e}")
                if "already been initialized" in msg or "already initialized" in msg:
                    _sleep(0.5)
                    continue
                # For other errors, re-raise after logging
                LOGGER.error(f"Failed to initialize spotdl client: {e}")
                raise
        # Final check
        if _GLOBAL_SPOTDL_CLIENT is None:
            raise RuntimeError("Unable to initialize spotdl client")


def reset_spotdl_client():
    """Reset the global Spotdl client to reinitialize with new credentials"""
    global _GLOBAL_SPOTDL_CLIENT
    with _GLOBAL_SPOTDL_LOCK:
        if _GLOBAL_SPOTDL_CLIENT is not None:
            _GLOBAL_SPOTDL_CLIENT = None
            LOGGER.info("Spotdl client reset. Will reinitialize on next use.")


def get_ffmpeg_path():
    """Get FFmpeg path - tries imageio-ffmpeg first, then system ffmpeg"""
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        ffmpeg_path = get_ffmpeg_exe()
        LOGGER.info(f"Using FFmpeg from imageio-ffmpeg: {ffmpeg_path}")
        return ffmpeg_path
    except ImportError:
        LOGGER.warning("imageio-ffmpeg not found, trying system ffmpeg")
        # Fallback to system ffmpeg or BinConfig
        return BinConfig.FFMPEG_NAME if hasattr(BinConfig, 'FFMPEG_NAME') else 'ffmpeg'


class MyLogger:
    def __init__(self, obj, listener):
        self._obj = obj
        self._listener = listener

    def debug(self, msg):
        LOGGER.debug(msg)

    def info(self, msg):
        LOGGER.info(msg)
        if "Downloaded" in msg:
            self._obj.playlist_count += 1

    def warning(self, msg):
        LOGGER.warning(msg)

    def error(self, msg):
        if msg != "ERROR: Cancelling...":
            LOGGER.error(msg)


class SpotdlHelper:
    def __init__(self, listener):
        self._last_downloaded = 0
        self._progress = 0
        self._downloaded_bytes = 0
        self._download_speed = 0
        self._eta = "-"
        self._listener = listener
        self._gid = ""
        self.is_playlist = False
        self.playlist_count = 0
        self.total_songs = 0
        
        # Spotdl client reference (may point to shared singleton)
        self.spotdl_client = None
        
    @property
    def download_speed(self):
        return self._download_speed

    @property
    def downloaded_bytes(self):
        return self._downloaded_bytes

    @property
    def size(self):
        return self._listener.size

    @property
    def progress(self):
        try:
            if self.total_songs > 0:
                return (self.playlist_count / self.total_songs) * 100
            return self._progress
        except:
            return 0

    @property
    def eta(self):
        return self._eta

    def _on_download_progress(self, progress_handler):
        """Callback for download progress"""
        if self._listener.is_cancelled:
            raise ValueError("Cancelling...")

    async def _on_download_start(self, from_queue=False):
        async with task_dict_lock:
            task_dict[self._listener.mid] = SpotdlStatus(self._listener, self, self._gid)
        if not from_queue:
            await self._listener.on_download_start()
            if self._listener.multi <= 1:
                await send_status_message(self._listener.message)

    def _on_download_error(self, error):
        self._listener.is_cancelled = True
        async_to_sync(self._listener.on_download_error, error)

    def _extract_meta_data(self, link):
        """Extract metadata from Spotify link"""
        try:
            # Decide qual arquivo de cookies usar (mesma lógica do módulo ytdlp)
            cookie_to_use = None
            try:
                usr_cookie = self._listener.user_dict.get("USER_COOKIE_FILE", "")
                use_default = self._listener.user_dict.get("USE_DEFAULT_COOKIE", False)
                if not use_default and usr_cookie and path.exists(usr_cookie):
                    cookie_to_use = usr_cookie
                elif path.exists("cookies.txt"):
                    cookie_to_use = "cookies.txt"
            except Exception:
                cookie_to_use = None

            # Inicializa/obtém cliente Spotdl compartilhado para evitar erro
            ffmpeg_path = get_ffmpeg_path()
            self.spotdl_client = get_spotdl_client(
                ffmpeg=ffmpeg_path,
                bitrate="320k",
                fmt="mp3",
                threads=4,
            )

            # Tentar injetar cookiefile nas opções do downloader/yt-dlp, se possível
            if cookie_to_use and hasattr(self.spotdl_client, "downloader"):
                try:
                    dl = self.spotdl_client.downloader
                    # vários nomes possíveis internalmente
                    if hasattr(dl, "ydl_opts") and isinstance(dl.ydl_opts, dict):
                        dl.ydl_opts["cookiefile"] = cookie_to_use
                    if hasattr(dl, "_ytdl_params") and isinstance(dl._ytdl_params, dict):
                        dl._ytdl_params["cookiefile"] = cookie_to_use
                except Exception as e:
                    LOGGER.warning(f"Could not set cookiefile in spotdl downloader: {e}")
            
            # Get songs from link
            songs = self.spotdl_client.search([link])

            # Tentar anexar um logger customizado para capturar mensagens do spotdl
            try:
                if hasattr(self.spotdl_client, "logger"):
                    self.spotdl_client.logger = MyLogger(self, self._listener)
                if hasattr(self.spotdl_client, "downloader"):
                    dl = self.spotdl_client.downloader
                    if hasattr(dl, "logger"):
                        dl.logger = MyLogger(self, self._listener)
                    elif hasattr(dl, "_logger"):
                        dl._logger = MyLogger(self, self._listener)
            except Exception as e:
                LOGGER.warning(f"Could not attach spotdl logger: {e}")
            
            if not songs:
                raise ValueError("No songs found in Spotify link")
            
            # ✅ CORREÇÃO 3: Contagem correta
            self.total_songs = len(songs)
            self.playlist_count = 0  # Reset counter
            
            # Check if playlist
            if len(songs) > 1:
                self.is_playlist = True
                
            # Calculate total size (estimate: 320kbps * duration)
            total_size = 0
            for song in songs:
                if hasattr(song, 'duration') and song.duration:
                    # 320kbps = 40KB/s, convert seconds to bytes
                    total_size += int(song.duration * 40000)
            
            self._listener.size = total_size if total_size > 0 else 1024 * 1024
            
            if not self._listener.name:
                if self.is_playlist:
                    if hasattr(songs[0], 'album_name') and songs[0].album_name:
                        self._listener.name = songs[0].album_name
                    elif hasattr(songs[0], 'list_name') and songs[0].list_name:
                        self._listener.name = songs[0].list_name
                    else:
                        self._listener.name = f"Spotify_Playlist_{self.total_songs}_songs"
                else:
                    # Single song
                    song = songs[0]
                    artist = song.artist if hasattr(song, 'artist') else 'Unknown'
                    name = song.name if hasattr(song, 'name') else 'Unknown'
                    self._listener.name = f"{artist} - {name}.mp3"
                    
            return songs
            
        except Exception as e:
            LOGGER.error(f"Error extracting metadata: {e}")
            self._on_download_error(str(e))
            return None

    def _download(self, path, songs):
        """Download songs using spotdl"""
        try:
            if not songs:
                raise ValueError("No songs to download")
            
            # ✅ CORREÇÃO 2: Criar diretório se não existir
            if not ospath.exists(path):
                makedirs(path, exist_ok=True)
                LOGGER.info(f"Created download directory: {path}")
            
            # Create output path for playlist
            if self.is_playlist:
                output_path = ospath.join(path, self._listener.name)
                makedirs(output_path, exist_ok=True)
            else:
                output_path = path
            
            LOGGER.info(f"Downloading {len(songs)} song(s) to: {output_path}")
            
            # ✅ Download songs one by one
            for idx, song in enumerate(songs, 1):
                if self._listener.is_cancelled:
                    LOGGER.info(f"Download cancelled by user at {idx}/{len(songs)}")
                    break
                    
                try:
                    LOGGER.info(f"[{idx}/{len(songs)}] Downloading: {song.name}")
                    
                    # Download individual song
                    try:
                        result, error = self.spotdl_client.downloader.download_song(
                            song, output=output_path
                        )
                    except TypeError:
                        # Older/newer spotdl API might not accept output arg;
                        # fallback: temporarily change cwd to output_path
                        prev_cwd = getcwd()
                        try:
                            # Antes de mudar o cwd, se existir cookie do usuário,
                            # copie para o diretório de saída como cookies.txt para
                            # garantir que o yt-dlp interno o encontre.
                            try:
                                if cookie_to_use and path.exists(cookie_to_use):
                                    dest_cookie = ospath.join(output_path, "cookies.txt")
                                    copyfile(cookie_to_use, dest_cookie)
                            except Exception as e:
                                LOGGER.warning(f"Could not copy cookie file to output dir: {e}")
                            chdir(output_path)
                            result, error = self.spotdl_client.downloader.download_song(
                                song
                            )
                        finally:
                            chdir(prev_cwd)
                    
                    if result:
                        self.playlist_count += 1
                        LOGGER.info(f"✅ [{self.playlist_count}/{len(songs)}] Downloaded: {song.name}")
                    else:
                        LOGGER.error(f"❌ Failed to download {song.name}: {error}")
                    
                except Exception as e:
                    LOGGER.error(f"❌ Error downloading {song.name}: {e}")
                    continue
            
            if self._listener.is_cancelled:
                return
            
            LOGGER.info(f"Download complete: {self.playlist_count}/{len(songs)} songs downloaded")
            # Log contents of expected output directories for debugging
            try:
                if ospath.exists(output_path):
                    files = [f for f in __import__("os").listdir(output_path)]
                    LOGGER.info(f"Files in output_path ({output_path}): {files}")
                else:
                    LOGGER.info(f"Expected output_path does not exist: {output_path}")
                if ospath.exists(path):
                    root_files = [f for f in __import__("os").listdir(path)]
                    LOGGER.info(f"Files in path ({path}): {root_files}")
                else:
                    LOGGER.info(f"Expected path does not exist: {path}")
            except Exception as e:
                LOGGER.warning(f"Could not list download dirs for debug: {e}")
            # ✅ Chamar on_download_complete SOMENTE se baixou algo
            if self.playlist_count > 0:
                async_to_sync(self._listener.on_download_complete)
            else:
                self._on_download_error("No songs were downloaded successfully")
            
        except Exception as e:
            LOGGER.error(f"Download error: {e}")
            if not self._listener.is_cancelled:
                self._on_download_error(str(e))
        finally:
            # Não destruímos o cliente singleton aqui, apenas removemos a
            # referência local — o client compartilhado vive no módulo.
            self.spotdl_client = None

    async def add_download(self, path):
        self._gid = token_hex(5)

        await self._on_download_start()

        # Extract metadata
        songs = await sync_to_async(self._extract_meta_data, self._listener.link)
        if not songs or self._listener.is_cancelled:
            return

        # Check for duplicates and limits
        msg, button = await stop_duplicate_check(self._listener)
        if msg:
            await self._listener.on_download_error(msg, button)
            return

        if limit_exceeded := await limit_checker(self._listener, self.total_songs):
            await self._listener.on_download_error(limit_exceeded, is_limit=True)
            return

        # Check if should queue
        add_to_queue, event = await check_running_tasks(self._listener)
        if add_to_queue:
            LOGGER.info(f"Added to Queue/Download: {self._listener.name}")
            async with task_dict_lock:
                task_dict[self._listener.mid] = QueueStatus(
                    self._listener, self._gid, "dl"
                )
            await event.wait()
            if self._listener.is_cancelled:
                return
            LOGGER.info(f"Start Queued Download from Spotify: {self._listener.name}")
            await self._on_download_start(True)

        if not add_to_queue:
            LOGGER.info(f"Download from Spotify: {self._listener.name}")

        # Start download
        await sync_to_async(self._download, path, songs)

    async def cancel_task(self):
        self._listener.is_cancelled = True
        LOGGER.info(f"Cancelling Spotify Download: {self._listener.name}")
        
        # Cleanup client
        if self.spotdl_client:
            with suppress(Exception):
                self.spotdl_client = None
        
        await self._listener.on_download_error("Stopped by User!")
