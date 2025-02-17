"""This module is used to emulate shuffling the track order of one or more
playlists. This is done by setting the track number attribute of each track in
sequential order after collecting the set of Tracks from the provided
playlist(s).
"""

import logging
import os
import random
from concurrent.futures import as_completed, ThreadPoolExecutor
from pathlib import Path
from typing import Optional, Type

from tqdm import tqdm

from djtools.collection.platform_registry import PLATFORM_REGISTRY
from djtools.utils.helpers import make_path


logger = logging.getLogger(__name__)
BaseConfig = Type["BaseConfig"]


@make_path
def shuffle_playlists(config: BaseConfig, path: Optional[Path] = None):
    """For each playlist in "shuffle_playlists", randomize the tracks and
    sequentially set the track number to emulate shuffling.

    Args:
        config: Configuration object.
        path: Path to write the new collection to.
    """
    # Load collection.
    collection = PLATFORM_REGISTRY[config.collection.platform]["collection"](
        path=config.collection.collection_path
    )

    # Build a dict of tracks to shuffle from the provided list of playlists.
    shuffled_tracks = {}
    for playlist_name in config.collection.shuffle_playlists:
        playlists = collection.get_playlists(playlist_name)
        if not playlists:
            raise LookupError(f"{playlist_name} not found")
        for playlist in playlists:
            tracks = playlist.get_tracks()
            track_keys = list(tracks.keys())
            random.shuffle(track_keys)
            shuffled_tracks.update({key: tracks[key] for key in track_keys})

    # Apply the shuffled track number to the attribute of the tracks.
    shuffled_tracks = list(shuffled_tracks.values())
    payload = [shuffled_tracks, list(range(1, len(shuffled_tracks) + 1))]
    with ThreadPoolExecutor(
        max_workers=os.cpu_count() * 4  # pylint: disable=no-member
    ) as executor:
        futures = [
            executor.submit(track.set_track_number, number)
            for track, number in zip(*payload)
        ]
        for future in tqdm(
            as_completed(futures),
            total=len(futures),
            desc=f"Randomizing {len(futures)} tracks",
        ):
            _ = future.result()

    # Insert a new playlist containing just the shuffled tracks.
    collection.add_playlist(
        PLATFORM_REGISTRY[config.collection.platform]["playlist"].new_playlist(
            name="SHUFFLE",
            tracks={track.get_id(): track for track in shuffled_tracks},
        )
    )
    _ = collection.serialize(path=path)
