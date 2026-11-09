import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from requests import HTTPError

from kemono_dl import KemonoDL
from kemono_dl.models import Creator, Post

TEST_DATA_PATH = "tests/data"


@pytest.fixture
def posts_and_creator():
    with open(f"{TEST_DATA_PATH}/post.json", encoding="utf-8") as f:
        post_data = json.load(f)
    with open(f"{TEST_DATA_PATH}/creator_profile.json", encoding="utf-8") as f:
        creator_data = json.load(f)

    post = Post(post_data)
    # The fixture repeats the same attachment many times. One attachment is
    # enough to exercise the preview fallback.
    post.attachments = post.attachments[:1]
    return post, Creator(**creator_data)


def test_post_maps_preview_to_attachment(posts_and_creator):
    post, _ = posts_and_creator
    attachment = post.attachments[0]

    assert attachment.preview_path is not None
    assert attachment.preview_name is not None


def test_pawchive_404_downloads_preview_and_writes_note(tmp_path, posts_and_creator):
    post, creator = posts_and_creator
    kemono_dl = KemonoDL(path=str(tmp_path))
    kemono_dl.get_post = Mock(return_value=post)
    kemono_dl.get_creator_profile = Mock(return_value=creator)

    calls = []

    def fake_download(session, url, filepath, temp_file=True):
        calls.append(url)
        if "/thumbnail/" not in url:
            raise HTTPError("404 Client Error", response=Mock(status_code=404))
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        Path(filepath).write_bytes(b"preview")

    with patch("kemono_dl.kemono_dl.download_file", side_effect=fake_download):
        kemono_dl.download_post(KemonoDL.PAWCHIVE_DOMAIN, post.service, post.user, post.id)

    note_path = kemono_dl.get_pawchive_preview_note_path(creator, post)
    assert note_path is not None
    assert Path(note_path).is_file()
    assert "HTTP 404" in Path(note_path).read_text(encoding="utf-8")
    assert calls[0].startswith("https://file.pawchive.pw/data")
    assert calls[1].startswith("https://file.pawchive.pw/thumbnail/data")


def test_archived_pawchive_post_with_note_is_retried(tmp_path, posts_and_creator):
    post, creator = posts_and_creator
    kemono_dl = KemonoDL(path=str(tmp_path), force_overwrite=False)
    kemono_dl.get_post = Mock(return_value=post)
    kemono_dl.get_creator_profile = Mock(return_value=creator)

    attachment = post.attachments[0]
    file_path = Path(
        kemono_dl.get_pawchive_preview_note_path(creator, post)
    ).parent / attachment.name
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(b"preview")

    note_path = kemono_dl.get_pawchive_preview_note_path(creator, post)
    Path(note_path).write_text("preview fallback", encoding="utf-8")
    kemono_dl.archived_posts = [f"{post.service}/user/{post.user}/post/{post.id}"]

    calls = []

    def fake_download(session, url, filepath, temp_file=True):
        calls.append(url)
        Path(filepath).write_bytes(b"original")

    def fake_sha256(filepath):
        if Path(filepath).read_bytes() == b"original":
            return attachment.path.split("/")[-1].split(".")[0]
        return "preview"

    with patch("kemono_dl.kemono_dl.download_file", side_effect=fake_download):
        with patch("kemono_dl.kemono_dl.get_sha256_hash", side_effect=fake_sha256):
            kemono_dl.download_post(KemonoDL.PAWCHIVE_DOMAIN, post.service, post.user, post.id)

    assert calls and calls[0].startswith("https://file.pawchive.pw/data")
    assert file_path.read_bytes() == b"original"
    assert not Path(note_path).exists()
