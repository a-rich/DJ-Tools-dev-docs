"""This module contains helper functions used by the "sync_operations" module.
Helper functions include formatting "aws s3 sync" commands, formatting the
output of "aws s3 sync" commands, posting uploaded tracks to Discord, and
modifying import_user's collection to point to tracks located at "usb_path".
"""

import logging
from datetime import datetime, timedelta
from itertools import groupby
from pathlib import Path
from subprocess import Popen, PIPE, CalledProcessError
from typing import Optional, Type

import requests

from djtools.collection.platform_registry import PLATFORM_REGISTRY
from djtools.utils.helpers import make_path


logger = logging.getLogger(__name__)
BaseConfig = Type["BaseConfig"]


def parse_sync_command(
    _cmd: str,
    config: BaseConfig,
    upload: Optional[bool] = False,
) -> str:
    """Appends flags to "aws s3 sync" command. If "*_include_dirs" is
        specified, all directories are ignored except those specified. If
        "*_exclude_dirs" is specified, all directories are included except
        those specified. Only one of these can be specified at once. If
        "aws_use_date_modified", then tracks will be
        re-downloaded / re-uploaded if their date modified at the source is
        after that of the destination.

    Args:
        _cmd: Partial "aws s3 sync" command.
        config: Configuration object.
        upload: Whether uploading or downloading.

    Returns:
        Fully constructed "aws s3 sync" command.
    """
    if (upload and config.sync.upload_include_dirs) or (
        not upload and config.sync.download_include_dirs
    ):
        _cmd.extend(["--exclude", "*"])
        directories = map(
            Path,
            getattr(
                config.sync, f'{"up" if upload else "down"}load_include_dirs'
            ),
        )
        for _dir in directories:
            path = Path(_dir.stem)
            ext = _dir.suffix
            if not ext:
                path = _dir / "*"
            else:
                path = _dir.parent / path.with_suffix(ext)
            _cmd.extend(["--include", path.as_posix()])
    if (upload and config.sync.upload_exclude_dirs) or (
        not upload and config.sync.download_exclude_dirs
    ):
        _cmd.extend(["--include", "*"])
        directories = map(
            Path,
            getattr(
                config.sync, f'{"up" if upload else "down"}load_exclude_dirs'
            ),
        )
        for _dir in directories:
            path = Path(_dir.stem)
            ext = _dir.suffix
            if not ext:
                path = _dir / "*"
            else:
                path = _dir.parent / path.with_suffix(ext)
            _cmd.extend(["--exclude", path.as_posix()])
    if not config.sync.aws_use_date_modified:
        _cmd.append("--size-only")
    if config.sync.dryrun:
        _cmd.append("--dryrun")
    logger.info(" ".join(_cmd))

    return _cmd


@make_path
def rewrite_track_paths(config: BaseConfig, other_user_collection: Path):
    """This function modifies the location of tracks in a collection.

    This is done by replacing the "usb_path" written by "import_user" with the
    "usb_path" in "config.yaml".

    Args:
        config: Configuration object.
        other_user_collection: Path to another user's collection.
    """
    music_path = Path("DJ Music")
    collection = PLATFORM_REGISTRY[config.collection.platform]["collection"](
        path=other_user_collection
    )
    for track in collection.get_tracks().values():
        loc = track.get_location().as_posix()
        common_path = (
            music_path / loc.split(str(music_path) + "/", maxsplit=-1)[-1]
        )
        track.set_location(config.sync.usb_path / common_path)
    collection.serialize(path=other_user_collection)


def run_sync(_cmd: str, bucket_url: str) -> str:
    """Runs subprocess for "aws s3 sync" command. Output is collected and
        formatted such that uploaded tracks are grouped by their directories.

    Args:
        _cmd: "aws s3 sync" command.
        bucket_url: URL to an AWS S3 API compliant bucket.

    Raises:
        CalledProcessError: raised if "aws s3 sync" command fails.
        RuntimeError: raised if any other exception occurs while syncing.

    Returns:
        Formatted list of uploaded tracks; tracks are grouped by directory.
    """
    line = ""
    termination_chars = {"\n", "\r"}
    tracks = []
    try:
        with Popen(_cmd, stdout=PIPE) as proc:
            while True:
                try:
                    char = proc.stdout.read(1).decode()
                except UnicodeDecodeError:
                    char = ""
                if char == "" and proc.poll() is not None:
                    break
                if char not in termination_chars:
                    line += char
                    continue
                print(line, end=char)
                if char != "\r" and "upload: " in line:
                    line = line.split(f"{bucket_url}/dj/music/")[-1]
                    tracks.append(Path(line))
                line = ""
            proc.stdout.close()
            return_code = proc.wait()
        if return_code:
            raise CalledProcessError(return_code, " ".join(_cmd))
    except Exception as exc:
        msg = f"Failure while syncing: {exc}"
        logger.critical(msg)
        raise RuntimeError(msg) from exc

    new_music = ""
    for group_id, group in groupby(
        sorted(tracks, key=lambda x: x.parent.as_posix()),
        key=lambda x: x.parent.as_posix(),
    ):
        group = sorted(group)
        new_music += f"{group_id}: {len(group)}\n"
        for track in group:
            new_music += f"\t{track.name}\n"
    if new_music:
        logger.info(
            f"Successfully uploaded {len(tracks)} tracks:\n{new_music}"
        )

    return new_music


@make_path
def upload_log(config: BaseConfig, log_file: Path):
    """This function uploads "log_file" to the "user" logs folder in S3. It
        then deletes all files created more than one day ago.

    Args:
        config: Configuration object.
        log_file: Path to log file.
    """
    if not config.sync.aws_profile:
        logger.warning(
            "Logs cannot be backed up without specifying the config option "
            "aws_profile"
        )
        return

    dst = (
        f"{config.sync.bucket_url}/dj/logs/{config.sync.user}/{log_file.name}"
    )
    cmd = ["aws", "s3", "cp", log_file.as_posix(), dst]
    logger.info(" ".join(cmd))
    with Popen(cmd) as proc:
        proc.wait()

    now = datetime.now()
    one_day = timedelta(days=1)
    for _file in log_file.parent.rglob("*"):
        if (
            _file.name != "__init__.py"
            and _file.is_file()
            and _file.lstat().st_mtime < (now - one_day).timestamp()
        ):
            _file.unlink()


def webhook(
    url: str, content_size_limit: int = 2000, content: Optional[str] = None
):
    """Post track list of newly uploaded tracks to Discord channel associated
        with "url". Track list is split across multiple messages if the
        character limit exceed "content_size_limit".

    Args:
        url (str): Discord URL for webhook.
        content_size_limit: Character limit for Discord message; if content is
            larger, then multiple messages are sent.
        content: Uploaded tracks (if any).
    """
    if not content:
        logger.info("There's no content")
        return

    batch = content[:content_size_limit]
    remainder = content[content_size_limit:]
    while batch:
        index = content_size_limit - 1
        while True:
            if index == 0:
                index = content_size_limit
                break
            try:
                if batch[index] == "\n":
                    break
            except IndexError:
                break
            index -= 1
        remainder = batch[index + 1 :] + remainder
        batch = batch[: index + 1]

        if batch:
            requests.post(url, json={"content": batch}, timeout=10)
            batch = remainder[:content_size_limit]
            remainder = remainder[content_size_limit:]
