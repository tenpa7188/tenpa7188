#!/usr/bin/env python3
"""Update selected GitHub Profile README sections from external feeds."""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

LOGGER = logging.getLogger(__name__)

MAX_PROJECTS = 2
MAX_ARTICLES = 3
PAGE_SIZE = 100
REQUEST_TIMEOUT_SECONDS = 20

DEFAULT_PROJECT_DESCRIPTION = "Repository description coming soon."
NO_PROJECTS_MESSAGE = "No projects found."
NO_ARTICLES_MESSAGE = "No articles found."

USER_AGENT = "profile-readme-updater/1.0"

README_PATH = Path(__file__).resolve().parent.parent / "README.md"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "profile_readme_config.json"
LATEST_PROJECTS_START = "<!--START_SECTION:latest_projects-->"
LATEST_PROJECTS_END = "<!--END_SECTION:latest_projects-->"
LATEST_ARTICLES_START = "<!--START_SECTION:latest_articles-->"
LATEST_ARTICLES_END = "<!--END_SECTION:latest_articles-->"

WHITESPACE_PATTERN = re.compile(r"\s+")
REQUIRED_CONFIG_KEYS = (
    "github_username",
    "zenn_username",
    "profile_repository_name",
)


@dataclass(frozen=True)
class ProfileConfig:
    """User-specific settings for the profile README updater."""

    github_username: str
    zenn_username: str
    profile_repository_name: str

    @property
    def github_api_url(self) -> str:
        """Return the GitHub repositories API endpoint."""
        return f"https://api.github.com/users/{self.github_username}/repos"

    @property
    def zenn_feed_url(self) -> str:
        """Return the Zenn RSS feed URL."""
        return f"https://zenn.dev/{self.zenn_username}/feed?all=1"


@dataclass(frozen=True)
class Project:
    """Display-ready GitHub repository data."""

    name: str
    html_url: str
    description: str
    updated_at: datetime


@dataclass(frozen=True)
class Article:
    """Display-ready Zenn article data."""

    title: str
    url: str
    published_at: datetime | None


def configure_logging() -> None:
    """Configure application logging."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def load_config(config_path: Path) -> ProfileConfig:
    """Load and validate the profile README configuration file."""
    if not config_path.is_file():
        raise FileNotFoundError(f"Configuration file was not found: {config_path}")

    try:
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Configuration file contains invalid JSON: {config_path}"
        ) from error

    if not isinstance(raw_config, dict):
        raise RuntimeError(
            "Configuration file must contain a JSON object at the top level."
        )

    normalized_values: dict[str, str] = {}
    for key in REQUIRED_CONFIG_KEYS:
        value = normalize_text(raw_config.get(key))
        if not value:
            raise RuntimeError(
                f"Configuration value '{key}' is missing or empty in {config_path}."
            )
        normalized_values[key] = value

    return ProfileConfig(
        github_username=normalized_values["github_username"],
        zenn_username=normalized_values["zenn_username"],
        profile_repository_name=normalized_values["profile_repository_name"],
    )


def normalize_text(value: object | None) -> str:
    """Collapse whitespace and trim leading or trailing spaces."""
    if value is None:
        return ""
    return WHITESPACE_PATTERN.sub(" ", str(value)).strip()


def escape_markdown_text(value: str) -> str:
    """Escape a small subset of Markdown-sensitive characters in text."""
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def parse_datetime(value: str | None) -> datetime | None:
    """Parse RFC 822 or ISO 8601 datetime strings into timezone-aware objects."""
    normalized_value = normalize_text(value)
    if not normalized_value:
        return None

    try:
        parsed = parsedate_to_datetime(normalized_value)
    except (TypeError, ValueError, IndexError, OverflowError):
        parsed = None

    if parsed is not None:
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    iso_value = normalized_value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_value)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def perform_request(url: str, headers: dict[str, str], context: str) -> bytes:
    """Fetch raw bytes from a URL with explicit error handling."""
    request = Request(url, headers=headers)

    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.read()
    except HTTPError as error:
        raise RuntimeError(
            f"{context} returned HTTP {error.code} for {url}."
        ) from error
    except socket.timeout as error:
        raise RuntimeError(
            f"{context} timed out after {REQUEST_TIMEOUT_SECONDS} seconds for {url}."
        ) from error
    except URLError as error:
        reason = error.reason
        if isinstance(reason, socket.timeout):
            raise RuntimeError(
                f"{context} timed out after {REQUEST_TIMEOUT_SECONDS} seconds for {url}."
            ) from error
        raise RuntimeError(f"{context} failed for {url}: {reason}.") from error


def github_headers() -> dict[str, str]:
    """Build request headers for GitHub API calls."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }

    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    return headers


def fetch_github_repositories(config: ProfileConfig) -> list[dict[str, Any]]:
    """Fetch all public repositories for the configured GitHub user."""
    repositories: list[dict[str, Any]] = []
    page = 1

    while True:
        query = urlencode(
            {
                "type": "owner",
                "sort": "updated",
                "per_page": PAGE_SIZE,
                "page": page,
            }
        )
        url = f"{config.github_api_url}?{query}"
        payload = perform_request(url, github_headers(), "GitHub API request")

        try:
            data = json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as error:
            raise RuntimeError("GitHub API returned invalid JSON.") from error

        if not isinstance(data, list):
            raise RuntimeError(
                "GitHub API returned an unexpected payload; expected a list of repositories."
            )

        repositories.extend(data)
        LOGGER.info("Fetched %s repositories from page %s.", len(data), page)

        if len(data) < PAGE_SIZE:
            break

        page += 1

    return repositories


def build_project_items(
    repositories: list[dict[str, Any]], profile_repository_name: str
) -> list[Project]:
    """Convert GitHub API payloads into filtered project objects."""
    projects: list[Project] = []

    for repository in repositories:
        name = normalize_text(repository.get("name"))
        html_url = normalize_text(repository.get("html_url"))

        if not name or not html_url:
            continue
        if repository.get("fork"):
            continue
        if repository.get("archived"):
            continue
        if name == profile_repository_name:
            continue

        description = normalize_text(repository.get("description"))
        updated_at = parse_datetime(repository.get("updated_at"))
        if updated_at is None:
            updated_at = datetime.min.replace(tzinfo=timezone.utc)

        projects.append(
            Project(
                name=name,
                html_url=html_url,
                description=description or DEFAULT_PROJECT_DESCRIPTION,
                updated_at=updated_at,
            )
        )

    projects.sort(key=lambda item: item.updated_at, reverse=True)
    return projects[:MAX_PROJECTS]


def build_latest_projects_section(projects: list[Project]) -> str:
    """Render the Latest Projects section body."""
    if not projects:
        return NO_PROJECTS_MESSAGE

    lines = [
        f"- [{escape_markdown_text(project.name)}]({project.html_url}) - "
        f"{escape_markdown_text(project.description)}"
        for project in projects
    ]
    return "\n".join(lines)


def local_name(tag: str) -> str:
    """Strip an XML namespace from a tag name."""
    return tag.rsplit("}", maxsplit=1)[-1]


def child_text(element: ET.Element, child_name: str) -> str | None:
    """Return the first child text that matches a local tag name."""
    for child in element:
        if local_name(child.tag) == child_name:
            return child.text
    return None


def child_attribute(
    element: ET.Element, child_name: str, attribute: str
) -> str | None:
    """Return the first child attribute that matches a local tag name."""
    for child in element:
        if local_name(child.tag) == child_name:
            return child.attrib.get(attribute)
    return None


def fetch_zenn_feed(config: ProfileConfig) -> ET.Element:
    """Fetch and parse the Zenn feed XML."""
    headers = {"User-Agent": USER_AGENT}
    payload = perform_request(config.zenn_feed_url, headers, "Zenn feed request")

    try:
        return ET.fromstring(payload)
    except ET.ParseError as error:
        raise RuntimeError("Zenn feed returned invalid XML.") from error


def parse_zenn_articles(feed_root: ET.Element) -> list[Article]:
    """Convert a Zenn RSS or Atom feed into article objects."""
    articles: list[Article] = []
    root_name = local_name(feed_root.tag)

    if root_name == "rss":
        channel = next(
            (child for child in feed_root if local_name(child.tag) == "channel"),
            None,
        )
        if channel is None:
            raise RuntimeError("Zenn RSS feed is missing a channel element.")

        for item in channel:
            if local_name(item.tag) != "item":
                continue

            title = normalize_text(child_text(item, "title"))
            link = normalize_text(child_text(item, "link"))
            published_at = parse_datetime(child_text(item, "pubDate"))

            if title and link:
                articles.append(Article(title=title, url=link, published_at=published_at))

    elif root_name == "feed":
        for entry in feed_root:
            if local_name(entry.tag) != "entry":
                continue

            title = normalize_text(child_text(entry, "title"))
            link = normalize_text(child_attribute(entry, "link", "href"))
            published_at = parse_datetime(child_text(entry, "published")) or parse_datetime(
                child_text(entry, "updated")
            )

            if title and link:
                articles.append(Article(title=title, url=link, published_at=published_at))

    else:
        raise RuntimeError(
            f"Unsupported Zenn feed format: unexpected root element '{feed_root.tag}'."
        )

    articles.sort(
        key=lambda item: item.published_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return articles[:MAX_ARTICLES]


def build_latest_articles_section(articles: list[Article]) -> str:
    """Render the Latest Articles section body."""
    if not articles:
        return NO_ARTICLES_MESSAGE

    lines = [
        f"- [{escape_markdown_text(article.title)}]({article.url})"
        for article in articles
    ]
    return "\n".join(lines)


def replace_section(
    readme_text: str, start_marker: str, end_marker: str, content: str
) -> str:
    """Replace the content between two markers while keeping the markers themselves."""
    start_index = readme_text.find(start_marker)
    if start_index == -1:
        raise ValueError(f"Start marker '{start_marker}' was not found in README.md.")

    content_start_index = start_index + len(start_marker)
    end_index = readme_text.find(end_marker, content_start_index)
    if end_index == -1:
        raise ValueError(f"End marker '{end_marker}' was not found in README.md.")

    replacement = f"{start_marker}\n{content.strip()}\n{end_marker}"
    return readme_text[:start_index] + replacement + readme_text[end_index + len(end_marker) :]


def update_readme(readme_path: Path, projects_content: str, articles_content: str) -> bool:
    """Update README sections and write only when the content actually changes."""
    if not readme_path.is_file():
        raise FileNotFoundError(f"README file was not found: {readme_path}")

    original_text = readme_path.read_text(encoding="utf-8")
    updated_text = replace_section(
        original_text,
        LATEST_PROJECTS_START,
        LATEST_PROJECTS_END,
        projects_content,
    )
    updated_text = replace_section(
        updated_text,
        LATEST_ARTICLES_START,
        LATEST_ARTICLES_END,
        articles_content,
    )

    if updated_text == original_text:
        return False

    with readme_path.open("w", encoding="utf-8", newline="\n") as file:
        file.write(updated_text)

    return True


def main() -> int:
    """Run the profile README update flow."""
    configure_logging()
    LOGGER.info("Starting README update.")

    try:
        config = load_config(CONFIG_PATH)
        repositories = fetch_github_repositories(config)
        projects = build_project_items(repositories, config.profile_repository_name)
        projects_section = build_latest_projects_section(projects)

        zenn_feed = fetch_zenn_feed(config)
        articles = parse_zenn_articles(zenn_feed)
        articles_section = build_latest_articles_section(articles)

        changed = update_readme(README_PATH, projects_section, articles_section)
    except Exception:
        LOGGER.exception("README update failed.")
        return 1

    if changed:
        LOGGER.info("README.md was updated successfully.")
    else:
        LOGGER.info("README.md is already up to date. No changes were written.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
