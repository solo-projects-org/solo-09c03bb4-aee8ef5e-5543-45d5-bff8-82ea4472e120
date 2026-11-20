"""Runtime configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_path: str
    origin_base_url: str
    request_timeout_s: float
    channel_config: str

    @property
    def channels(self) -> list[dict]:
        """Channel definitions, as ``id|name|master_url`` triplets.

        The defaults point at the bundled Compose demo origin.  Deployments can
        override ``CHANNEL_CONFIG`` with their own list.
        """
        entries = [item.strip() for item in self.channel_config.split(",") if item.strip()]
        channels = []
        seen: set[str] = set()
        for entry in entries:
            parts = [part.strip() for part in entry.split("|")]
            if len(parts) != 3 or not all(parts):
                raise ValueError(
                    "CHANNEL_CONFIG entries must look like id|name|master_url"
                )
            channel_id, name, master_url = parts
            if channel_id in seen:
                raise ValueError(f"duplicate channel id in CHANNEL_CONFIG: {channel_id}")
            seen.add(channel_id)
            channels.append({"id": channel_id, "name": name, "master_url": master_url})
        return channels


def load_settings() -> Settings:
    return Settings(
        database_path=os.environ.get("ARCHIVE_DB", "./data/archive.sqlite3"),
        origin_base_url=os.environ.get("ORIGIN_BASE_URL", "http://origin:8001").rstrip("/"),
        request_timeout_s=float(os.environ.get("ORIGIN_TIMEOUT_S", "10")),
        channel_config=os.environ.get(
            "CHANNEL_CONFIG",
            "news|新闻综合|http://origin:8001/channel/news/master.m3u8,"
            "sports|体育赛事|http://origin:8001/channel/sports/master.m3u8",
        ),
    )
