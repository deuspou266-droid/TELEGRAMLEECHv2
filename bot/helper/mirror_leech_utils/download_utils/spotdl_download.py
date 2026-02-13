from logging import getLogger
from os import makedirs, getcwd, chdir
from os import path as ospath  # único alias para os.path — sem "path" solto
from shutil import rmtree, copyfile
from secrets import token_hex
from contextlib import suppress

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

    with _GLOBAL_SPOTDL_LOCK:
        if _GLOBAL_SPOTDL_CLIENT is not None:
            return _GLOBAL_SPOTDL_CLIENT

        client_id = Config.SPOTIFY_CLIENT_ID or None
        client_secret = Config.SPOTIFY_CLIENT_SECRET or None

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
                msg = str(e).lower()
                LOGGER.warning(f"Spotdl init attempt {attempt+1} failed: {e}")
                if "already been initialized" in msg or "already initialized" in msg:
                    _sleep(0.5)
                    continue
                LOGGER.error(f"Failed to initialize spotdl client: {e}")
                raise

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
        return BinConfig.FFMPEG_NAME if hasattr(BinConfig, 'FFMPEG_NAME') else 'ffmpeg'


class MyLogger:
    def __init__(self, obj, listener):
        self._obj = obj
        self._listener = listener

    def debug(self, msg):
        LOGGER.debug(msg)

    def info(self, msg):
        LOGGER.info(msg)
        # FIX: só conta como downloaded se for mensagem de sucesso real do spotdl,
        # não qualquer mensagem que contenha a palavra "Downloaded"
        if msg.startswith("Downloaded") or " - Downloaded" in msg:
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
        self.spotdl_client = None
        # FIX: cookie_to_use como atributo da instância para ser acessível
        # em _download() sem depender do escopo de _extract_meta_data()
        self.cookie_to_use = None

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
            # FIX: usa self.cookie_to_use em vez de variável local,
            # para que _download() possa acessá-la depois.
            # FIX: usa ospath.exists() em vez de path.exists()
            # (o nome "path" não é mais importado solto)
            self.cookie_to_use = None
            try:
                usr_cookie = self._listener.user_dict.get("USER_COOKIE_FILE", "")
                use_default = self._listener.user_dict.get("USE_DEFAULT_COOKIE", False)
                if not use_default and usr_cookie and ospath.exists(usr_cookie):
                    self.cookie_to_use = usr_cookie
                elif ospath.exists("cookies.txt"):
                    self.cookie_to_use = "cookies.txt"
            except Exception:
                self.cookie_to_use = None

            if self.cookie_to_use:
                LOGGER.info(f"Using cookie file: {self.cookie_to_use}")
            else:
                LOGGER.warning("No cookie file found. Download may fail on restricted content.")

            ffmpeg_path = get_ffmpeg_path()
            self.spotdl_client = get_spotdl_client(
                ffmpeg=ffmpeg_path,
                bitrate="320k",
                fmt="mp3",
                threads=4,
            )

            # Injetar cookiefile nas opções internas do yt-dlp do spotdl
            if self.cookie_to_use and hasattr(self.spotdl_client, "downloader"):
                try:
                    dl = self.spotdl_client.downloader
                    if hasattr(dl, "ydl_opts") and isinstance(dl.ydl_opts, dict):
                        dl.ydl_opts["cookiefile"] = self.cookie_to_use
                        LOGGER.info("Injected cookiefile into ydl_opts")
                    if hasattr(dl, "_ytdl_params") and isinstance(dl._ytdl_params, dict):
                        dl._ytdl_params["cookiefile"] = self.cookie_to_use
                        LOGGER.info("Injected cookiefile into _ytdl_params")
                except Exception as e:
                    LOGGER.warning(f"Could not set cookiefile in spotdl downloader: {e}")

            songs = self.spotdl_client.search([link])

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

            self.total_songs = len(songs)
            self.playlist_count = 0

            if len(songs) > 1:
                self.is_playlist = True

            total_size = 0
            for song in songs:
                if hasattr(song, 'duration') and song.duration:
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
                    song = songs[0]
                    artist = song.artist if hasattr(song, 'artist') else 'Unknown'
                    name = song.name if hasattr(song, 'name') else 'Unknown'
                    self._listener.name = f"{artist} - {name}.mp3"

            return songs

        except Exception as e:
            LOGGER.error(f"Error extracting metadata: {e}")
            self._on_download_error(str(e))
            return None

    def _download(self, dl_path, songs):
        """Download songs using spotdl.
        
        Nota: o argumento foi renomeado de 'path' para 'dl_path' para evitar
        sombreamento do módulo ospath importado no topo do arquivo.
        """
        try:
            if not songs:
                raise ValueError("No songs to download")

            if not ospath.exists(dl_path):
                makedirs(dl_path, exist_ok=True)
                LOGGER.info(f"Created download directory: {dl_path}")

            if self.is_playlist:
                output_path = ospath.join(dl_path, self._listener.name)
                makedirs(output_path, exist_ok=True)
            else:
                output_path = dl_path

            LOGGER.info(f"Downloading {len(songs)} song(s) to: {output_path}")

            for idx, song in enumerate(songs, 1):
                if self._listener.is_cancelled:
                    LOGGER.info(f"Download cancelled by user at {idx}/{len(songs)}")
                    break

                try:
                    LOGGER.info(f"[{idx}/{len(songs)}] Downloading: {song.name}")

                    try:
                        result, error = self.spotdl_client.downloader.download_song(
                            song, output=output_path
                        )
                    except TypeError:
                        # API do spotdl desta versão não aceita output= como argumento;
                        # fallback: muda cwd temporariamente para output_path.
                        prev_cwd = getcwd()
                        try:
                            # FIX: usa self.cookie_to_use (atributo da instância)
                            # FIX: usa ospath.exists() em vez de path.exists()
                            if self.cookie_to_use and ospath.exists(self.cookie_to_use):
                                dest_cookie = ospath.join(output_path, "cookies.txt")
                                copyfile(self.cookie_to_use, dest_cookie)
                                LOGGER.info(f"Copied cookie file to output dir: {dest_cookie}")
                            chdir(output_path)
                            result, error = self.spotdl_client.downloader.download_song(song)
                        except Exception as e:
                            LOGGER.warning(f"Could not copy cookie file to output dir: {e}")
                            chdir(output_path)
                            result, error = self.spotdl_client.downloader.download_song(song)
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

            try:
                if ospath.exists(output_path):
                    files = [f for f in __import__("os").listdir(output_path)]
                    LOGGER.info(f"Files in output_path ({output_path}): {files}")
                if ospath.exists(dl_path):
                    root_files = [f for f in __import__("os").listdir(dl_path)]
                    LOGGER.info(f"Files in dl_path ({dl_path}): {root_files}")
            except Exception as e:
                LOGGER.warning(f"Could not list download dirs for debug: {e}")

            if self.playlist_count > 0:
                async_to_sync(self._listener.on_download_complete)
            else:
                self._on_download_error("No songs were downloaded successfully")

        except Exception as e:
            LOGGER.error(f"Download error: {e}")
            if not self._listener.is_cancelled:
                self._on_download_error(str(e))
        finally:
            self.spotdl_client = None

    async def add_download(self, dl_path):
        self._gid = token_hex(5)

        await self._on_download_start()

        songs = await sync_to_async(self._extract_meta_data, self._listener.link)
        if not songs or self._listener.is_cancelled:
            return

        msg, button = await stop_duplicate_check(self._listener)
        if msg:
            await self._listener.on_download_error(msg, button)
            return

        if limit_exceeded := await limit_checker(self._listener, self.total_songs):
            await self._listener.on_download_error(limit_exceeded, is_limit=True)
            return

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

        await sync_to_async(self._download, dl_path, songs)

    async def cancel_task(self):
        self._listener.is_cancelled = True
        LOGGER.info(f"Cancelling Spotify Download: {self._listener.name}")

        if self.spotdl_client:
            with suppress(Exception):
                self.spotdl_client = None

        await self._listener.on_download_error("Stopped by User!")
