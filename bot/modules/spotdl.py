from aiofiles.os import path as aiopath

from .. import DOWNLOAD_DIR, LOGGER, bot_loop
from ..helper.ext_utils.bot_utils import (
    COMMAND_USAGE,
    arg_parser,
    new_task,
)
from ..helper.ext_utils.links_utils import is_url
from ..helper.ext_utils.task_manager import pre_task_check
from ..helper.listeners.task_listener import TaskListener
from ..helper.mirror_leech_utils.download_utils.spotdl_download import SpotdlHelper
from ..helper.telegram_helper.message_utils import (
    auto_delete_message,
    delete_links,
    send_message,
)


class Spotdl(TaskListener):
    def __init__(
        self,
        client,
        message,
        is_leech=False,
        same_dir=None,
        bulk=None,
        multi_tag=None,
        options="",
        **kwargs,
    ):
        if same_dir is None:
            same_dir = {}
        if bulk is None:
            bulk = []
        self.message = message
        self.client = client
        self.multi_tag = multi_tag
        self.options = options
        self.same_dir = same_dir
        self.bulk = bulk
        super().__init__()
        self.is_spotdl = True
        self.is_leech = is_leech

    async def new_event(self):
        text = self.message.text.split("\n")
        input_list = text[0].split(" ")

        check_msg, check_button = await pre_task_check(self.message)
        if check_msg:
            await delete_links(self.message)
            await auto_delete_message(
                await send_message(self.message, check_msg, check_button)
            )
            return

        args = {
            "-doc": False,
            "-med": False,
            "-b": False,
            "-z": False,
            "-i": 0,
            "-sp": 0,
            "link": "",
            "-m": "",
            "-n": "",
            "-up": "",
            "-rcf": "",
            "-t": "",
        }

        arg_parser(input_list[1:], args)

        try:
            self.multi = int(args["-i"])
        except Exception:
            self.multi = 0

        self.name = args["-n"]
        self.up_dest = args["-up"]
        self.rc_flags = args["-rcf"]
        self.link = args["link"]
        self.compress = args["-z"]
        self.thumb = args["-t"]
        self.split_size = args["-sp"]
        self.as_doc = args["-doc"]
        self.as_med = args["-med"]
        self.folder_name = f"/{args['-m']}".rstrip("/") if len(args["-m"]) > 0 else ""

        is_bulk = args["-b"]
        bulk_start = 0
        bulk_end = 0
        reply_to = None

        if not isinstance(is_bulk, bool):
            dargs = is_bulk.split(":")
            bulk_start = dargs[0] or None
            if len(dargs) == 2:
                bulk_end = dargs[1] or None
            is_bulk = True

        if is_bulk:
            await self.init_bulk(input_list, bulk_start, bulk_end, Spotdl)
            return

        if len(self.bulk) != 0:
            del self.bulk[0]

        path = f"{DOWNLOAD_DIR}{self.mid}{self.folder_name}"

        await self.get_tag(text)

        if not self.link and (reply_to := self.message.reply_to_message):
            self.link = reply_to.text.split("\n", 1)[0].strip()

        # Validate Spotify link
        if not is_url(self.link) or not any(
            x in self.link for x in ["spotify.com/track", "spotify.com/album", "spotify.com/playlist"]
        ):
            await send_message(
                self.message,
                "Please provide a valid Spotify track, album, or playlist link.\n\n"
                "Example: https://open.spotify.com/track/...",
            )
            await self.remove_from_same_dir()
            await delete_links(self.message)
            return

        try:
            await self.before_start()
        except Exception as e:
            await send_message(self.message, e)
            await self.remove_from_same_dir()
            await delete_links(self.message)
            return

        self._set_mode_engine()

        LOGGER.info(f"Downloading from Spotify: {self.link}")
        spotdl = SpotdlHelper(self)
        await delete_links(self.message)
        await spotdl.add_download(path)
        await self.run_multi(input_list, Spotdl)

        # Removido: tentativa falha de buscar cookies.txt do navegador
        # O yt-dlp agora usa o servidor POT via --extractor-args

async def spotdl(client, message):
    bot_loop.create_task(Spotdl(client, message).new_event())


async def spotdl_leech(client, message):
    bot_loop.create_task(Spotdl(client, message, is_leech=True).new_event())
