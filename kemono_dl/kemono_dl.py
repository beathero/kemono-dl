import http.cookiejar

# import mimetypes
import json
import os
import re
import time
from http.cookiejar import LoadError
from typing import List, Literal
from urllib.parse import urljoin

from requests.exceptions import HTTPError, RequestException

from .downloader import download_file
from .models import Attachment, Creator, FavoriteCreator, FileTemplateVaribales, Post
from .session import CustomSession
from .utils import compute_sha256, generate_file_path, get_sha256_hash  # , get_sha256_url_content

OverwriteMode = Literal[False, "soft", True]
# "soft" will not overwrite the file if it has the expected sha256 hash
# NOTE: if two attachments have the same name (and in the same directory) but different sha256 hashes it will overwirte the first file with the second


class KemonoDL:
    COOMER_DOMAIN = "https://coomer.st"
    KEMONO_DOMAIN = "https://kemono.cr"
    PAWCHIVE_DOMAIN = "https://pawchive.pw"
    PAWCHIVE_FILE_DOMAIN = "https://file.pawchive.pw"
    PAWCHIVE_PREVIEW_NOTE = "pawchive_preview_fallback.txt"
    POST_STEP_SIZE = 50
    URL_PARSE_PATTERN = r"^https://(kemono|coomer|pawchive)\.\w+/([^/]+)/user/([^/]+)(?:/post/([^/]+))?$"
    DEFAULT_OUTPUT_TEMPLATE = "{service}/{creator_id}/{post_id}/{filename}"

    def __init__(
        self,
        path: str = os.getcwd(),
        output_templates: dict = {
            "attachments": DEFAULT_OUTPUT_TEMPLATE,
            # "pfp": DEFAULT_OUTPUT_TEMPLATE,
            # "banner": DEFAULT_OUTPUT_TEMPLATE,
            "content": DEFAULT_OUTPUT_TEMPLATE,
            "json": DEFAULT_OUTPUT_TEMPLATE,
        },
        restrict_names: bool = False,
        custom_template_variables: dict = {},
        archive_file: str | None = None,
        force_overwrite: OverwriteMode = "soft",
        max_retries: int = 3,
        post_filters: dict = {},
        attachment_filters: dict = {},
        skip_attachments: bool = False,
        write_content: bool = False,
        write_json: bool = False,
        no_tmp: bool = False,
    ) -> None:
        self.domain = KemonoDL.COOMER_DOMAIN
        self.session = CustomSession()
        self.creators_cache: dict[tuple[str, str], Creator] = {}
        self.path = path
        self.output_templates = output_templates
        self.restrict_names = restrict_names
        self.custom_template_variables = custom_template_variables
        self.force_overwrite = force_overwrite
        self.max_retries = max_retries
        self.post_filters = post_filters
        self.attachment_filters = attachment_filters
        self.skip_attachments = skip_attachments
        self.write_content = write_content
        self.write_json = write_json
        self.no_tmp = no_tmp

        self.archive_file = archive_file
        self.archived_posts = []
        self.load_archive_file()

    def load_archive_file(self) -> None:
        if self.archive_file and os.path.isfile(self.archive_file):
            with open(self.archive_file, "r") as f:
                self.archived_posts.extend(f"{parsed_url['service']}/user/{parsed_url['creator_id']}/post/{parsed_url['post_id']}" for line in f if (parsed_url := self.parse_url(line.strip())))

    def write_archive_file(self, domain: str, service: str, creator_id: str, post_id: str) -> None:
        archive_data = f"{domain}/{service}/user/{creator_id}/post/{post_id}"
        self.archived_posts.append(archive_data)
        if self.archive_file:
            if os.path.exists(self.archive_file):
                with open(self.archive_file, "a") as f:
                    f.write(archive_data + "\n")
            else:
                with open(self.archive_file, "w") as f:
                    f.write(archive_data + "\n")

    def parse_url(self, url) -> dict | None:
        match = re.match(KemonoDL.URL_PARSE_PATTERN, url)
        if match:
            site, service, creator_id, post_id = match.groups()
            return {"site": site, "service": service, "creator_id": creator_id, "post_id": post_id}
        return None

    def load_cookies(self, cookies_file: str) -> bool:
        try:
            jar = http.cookiejar.MozillaCookieJar()
            jar.load(cookies_file)
            for cookie in jar:
                self.session.cookies.set_cookie(cookie)
            return True
        except (LoadError, OSError) as e:
            print(f"[Error] Failed to load cookies from {cookies_file}: {e}")
            return False

    def login(self, domain: str, username: str, password: str) -> bool:
        try:
            if KemonoDL.PAWCHIVE_DOMAIN:
                url = f"{domain}/account/login"
                response = self.session.post(url, data={"location": "/artists", "username": username, "password": password})
                response.raise_for_status()
                return True
            else:
                url = f"{domain}/api/v1/authentication/login"
                response = self.session.post(url, json={"username": username, "password": password})
                response.raise_for_status()
                return True
        except RequestException as e:
            print(f"[Error] Unable to login: {e}")
            return False

    def isLoggedin(self, domain: str) -> bool:
        if KemonoDL.PAWCHIVE_DOMAIN:
            url = f"{domain}/account"
            response = self.session.get(url)
            return response.ok
        else:
            url = f"{domain}/api/v1/account"
            response = self.session.get(url, headers={"accept": "text/css"})
            return response.ok

    def get_creator_profile(self, domain: str, service: str, creator_id: str) -> Creator | None:
        try:
            creator = self.creators_cache.get((service, creator_id), None)
            if creator is None:
                url = f"{domain}/api/v1/{service}/user/{creator_id}/profile"
                response = self.session.get(url, headers={"accept": "text/css"})
                response.raise_for_status()
                creator = Creator(**response.json())
                self.creators_cache[(service, creator_id)] = creator
            return creator
        except (RequestException, ValueError) as e:
            print(f"[Error] Failed to fetch creator profile from {url!r}: {e}")
            return None

    def get_creator_post_ids(self, domain: str, service: str, creator_id: str, offset: int = 0) -> list[str]:
        try:
            url = f"{domain}/api/v1/{service}/user/{creator_id}/posts"
            response = self.session.get(url, params={"o": offset}, headers={"accept": "text/css"})
            response.raise_for_status()
            posts = response.json()
            return [post.get("id") for post in posts]
        except (RequestException, ValueError) as e:
            print(f"[Error] Failed to fetch posts from {url!r}: {e}")
            return []

    def get_all_creator_post_ids(self, domain: str, service: str, creator_id: str, limit: int = 0, offset: int = 0) -> list[str]:
        posts = []
        while True:
            posts_chunk = self.get_creator_post_ids(domain, service, creator_id, offset)
            posts += posts_chunk
            if len(posts) >= limit and limit > 0:
                posts = posts[:limit]
                break
            if len(posts_chunk) < KemonoDL.POST_STEP_SIZE:
                break
            offset += KemonoDL.POST_STEP_SIZE
            time.sleep(0.5)
        return posts

    def get_post(self, domain: str, service: str, creator_id: str, post_id: str) -> Post | None:
        try:
            url = f"{domain}/api/v1/{service}/user/{creator_id}/post/{post_id}"
            response = self.session.get(url, headers={"accept": "text/css"})
            response.raise_for_status()
            post_api = response.json()
            return Post(post_api)
        except (RequestException, ValueError) as e:
            print(f"[Error] Failed to fetch post from {url!r}: {e}")
            return None

    def get_favorit_creators(self, domain: str) -> List[FavoriteCreator] | None:
        try:
            url = f"{domain}/api/v1/account/favorites"
            response = self.session.get(url, params={"type": "artist"}, headers={"accept": "text/css"})
            response.raise_for_status()
            creators = response.json()
            return [FavoriteCreator(**creator) for creator in creators]
        except (RequestException, ValueError) as e:
            print(f"[Error] Failed to fetch favorite creators from {url!r}: {e}")
            return None

    def get_favorit_post_ids(self, domain: str) -> List[str] | None:
        try:
            url = f"{domain}/api/v1/account/favorites"
            response = self.session.get(url, params={"type": "post"}, headers={"accept": "text/css"})
            response.raise_for_status()
            posts = response.json()
            return [post.get("id") for post in posts]
        except (RequestException, ValueError) as e:
            print(f"[Error] Failed to fetch favorite posts from {url!r}: {e}")
            return None

    def download_favorite_creators(self, domain: str) -> None:
        if not self.isLoggedin(domain):
            print(f"[Error] You are not logged into {domain!r}")
            return

        creators = self.get_favorit_creators(domain)

        if creators is None:
            return

        for creator in creators:
            post_ids = self.get_all_creator_post_ids(domain, creator.service, creator.id)
            for post_id in post_ids:
                time.sleep(0.5)
                self.download_post(domain, creator.service, creator.id, post_id)

    def download_favorite_posts(self, domain: str):
        pass

    def download_url(self, url: str) -> None:
        parsed_url = self.parse_url(url)

        if parsed_url is None:
            print("Invalid URL:" + url)
            return

        if parsed_url["site"] == "kemono":
            domain = KemonoDL.KEMONO_DOMAIN
        elif parsed_url["site"] == "coomer":
            domain = KemonoDL.COOMER_DOMAIN
        elif parsed_url["site"] == "pawchive":
            domain = KemonoDL.PAWCHIVE_DOMAIN

        if parsed_url["post_id"]:
            self.download_post(domain, parsed_url["service"], parsed_url["creator_id"], parsed_url["post_id"])
        else:
            post_ids = self.get_all_creator_post_ids(domain, parsed_url["service"], parsed_url["creator_id"])
            for post_id in post_ids:
                time.sleep(0.5)
                self.download_post(domain, parsed_url["service"], parsed_url["creator_id"], post_id)

    def download_creator_banner(self, domain: str, service: str, creator_id: str) -> None:
        self._download_special(domain, service, creator_id, "banner")

    def download_creator_icon(self, domain: str, service: str, creator_id: str) -> None:
        self._download_special(domain, service, creator_id, "icon")

    def _download_special(self, domain: str, service: str, creator_id: str, type: str) -> None:
        pass
        # creator = self.get_creator_profile(domain, service, creator_id)
        # if creator is None:
        #     return
        # url = self.domain + f"{type}s/{service}/{creator_id}"
        # response = self.session.head(url, allow_redirects=True)
        # contentType = response.headers.get("Content-Type", "")
        # ext = (mimetypes.guess_extension(contentType) or ".bin")[1:]

        # sha256 = get_sha256_url_content(self.session, url) if "{sha256}" in self.output_templates else ""

        # file_path = generate_file_path(
        #     self.path,
        #     self.output_template_special,
        #     {
        #         "service": creator.service,
        #         "creator_id": creator.id,
        #         "filename": creator_id + "." + ext,
        #         "file_name": creator_id,
        #         "file_ext": ext,
        #         "type": type,
        #         "sha256": sha256,
        #     },
        #     self.restrict_names,
        # )

        # download_file(
        #     session=self.session,
        #     url=url,
        #     filepath=file_path,
        # )

    def download_post(self, domain: str, service: str, creator_id: str, post_id: str) -> None:
        print(f"[downloading] Post URL: {domain}/{service}/user/{creator_id}/post/{post_id}")

        archive_key = f"{service}/user/{creator_id}/post/{post_id}"
        is_archived = archive_key in self.archived_posts

        # Pawchive posts can be archived while some files were only available as
        # previews. The preview note makes those posts eligible for another
        # download attempt, even when the post is already in the archive.
        if is_archived and domain != KemonoDL.PAWCHIVE_DOMAIN:
            print(f"[info] Post {post_id!r} already archived. Skipping.")
            return

        post = self.get_post(domain, service, creator_id, post_id)

        if post is None:
            return

        if self.post_matches_filters(post):
            print(f"[info] Post {post.id!r} matched 1 or more post filters. Skipping.")
            return

        printable_title = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", post.title)[:50]
        print(f"[downloading] Post: {printable_title}")

        creator = self.get_creator_profile(domain, post.service, post.user)
        if creator is None:
            return

        preview_note_paths = self.get_pawchive_preview_note_paths(creator, post) if domain == KemonoDL.PAWCHIVE_DOMAIN else set()
        existing_preview_note_paths = [note_path for note_path in preview_note_paths if os.path.isfile(note_path)]

        if is_archived and not existing_preview_note_paths:
            print(f"[info] Post {post_id!r} already archived. Skipping.")
            return

        if existing_preview_note_paths:
            print(f"[info] Found preview fallback note at {existing_preview_note_paths[0]!r}. Retrying original attachments.")

        post_errors = 0

        if self.write_json:
            try:
                self.write_post_json(creator, post)
            except Exception as e:
                print(f"[Error] Failed to write post json: {e}")
                post_errors += 1

        if self.write_content:
            try:
                self.write_post_content(creator, post)
            except Exception as e:
                print(f"[Error] Failed to write post content: {e}")
                post_errors += 1

        if self.skip_attachments:
            print("[info] Skipping Post attachments.")
        else:
            post_errors += self.download_post_attachments(domain, creator, post)

        if post_errors == 0 and not is_archived:
            self.write_archive_file(domain, post.service, post.user, post.id)

    def download_post_attachments(self, domain: str, creator: Creator, post: Post):
        if not post.attachments:
            return 0

        print(f"[downloading] Attachments: {len(post.attachments)}")

        attachment_errors = 0
        fallback_used = False
        skipped_attachment = False
        preview_note_paths: set[str] = set()

        for attachment in post.attachments:
            if self.attachment_matches_filters(attachment):
                print("[info] Attachment matched 1 or more attachment filters. Skipping.")
                skipped_attachment = True
                continue

            template_variables = FileTemplateVaribales(creator, post, attachment)

            file_path = generate_file_path(
                self.path,
                self.output_templates.get("attachments", self.DEFAULT_OUTPUT_TEMPLATE),
                template_variables.toDict(self.custom_template_variables),
                self.restrict_names,
            )
            expected_sha256 = template_variables.sha256
            preview_note_path = self.get_pawchive_preview_note_path_from_file_path(file_path) if domain == KemonoDL.PAWCHIVE_DOMAIN else None
            if preview_note_path and os.path.isfile(preview_note_path):
                preview_note_paths.add(preview_note_path)

            if os.path.exists(file_path):
                actual_sha256 = get_sha256_hash(file_path)
                retrying_preview = bool(preview_note_path and os.path.isfile(preview_note_path))

                if expected_sha256 == actual_sha256:
                    print(f"[info] File already exists with matching sha256 at {file_path}")
                    continue

                if self.force_overwrite is False and not retrying_preview:
                    print(f"[info] File already exists at {file_path}")
                    print(f'[warning] File sha256 mismatch. Expected "{expected_sha256}" recieved"{actual_sha256}"')
                    continue

                if retrying_preview:
                    print(f"[info] Replacing preview with an original attachment at {file_path}")

            os.makedirs(os.path.dirname(file_path), exist_ok=True)

            if domain == KemonoDL.PAWCHIVE_DOMAIN:
                url = f"{self.PAWCHIVE_FILE_DOMAIN}/data{attachment.path}?f={attachment.name}"
            else:
                url = f"{attachment.server}/data{attachment.path}"

            downloaded = False
            downloaded_preview = False
            original_returned_404 = False

            for attempt in range(self.max_retries):
                try:
                    download_file(self.session, url, file_path, temp_file=not self.no_tmp)
                    downloaded = True
                    break
                except HTTPError as e:
                    print(f"[Error] Failed to download attachment from {url!r}: {e}")
                    if domain == KemonoDL.PAWCHIVE_DOMAIN and self.is_http_404(e):
                        original_returned_404 = True
                        webpage_url = f"{domain}/{post.service}/user/{post.user}/post/{post.id}"
                        if self.download_pawchive_preview(attachment, file_path, url, webpage_url=webpage_url):
                            downloaded = True
                            downloaded_preview = True
                        # A 404 is permanent, so there is no point in retrying
                        # the original attachment URL.
                        break
                except Exception as e:
                    print(f"[Error] Failed to download attachment from {url!r}: {e}")

            if not downloaded:
                if original_returned_404:
                    print(f"[Error] Original attachment returned HTTP 404 and no preview fallback URL worked for {attachment.name!r}")
                else:
                    print(f"[Error] All {self.max_retries} download retries failed")
                attachment_errors += 1
                continue

            if downloaded_preview:
                fallback_used = True
                continue

            actual_sha256 = get_sha256_hash(file_path)
            if expected_sha256 != actual_sha256:
                print(f"[Error] File downloaded with incorrect SHA-256. Expected: {expected_sha256} Actual: {actual_sha256}")
                attachment_errors += 1

        # Only remove a fallback note once every attachment has been replaced
        # with its original and no attachment was skipped by a filter.
        if domain == KemonoDL.PAWCHIVE_DOMAIN and preview_note_paths and not fallback_used and not skipped_attachment and attachment_errors == 0:
            for note_path in preview_note_paths:
                try:
                    os.remove(note_path)
                    print(f"[info] Removed preview fallback note at {note_path!r}")
                except OSError as e:
                    print(f"[Error] Failed to remove preview fallback note at {note_path!r}: {e}")

        return attachment_errors

    @staticmethod
    def is_http_404(error: HTTPError) -> bool:
        response = getattr(error, "response", None)
        return bool(response is not None and response.status_code == 404)

    def get_pawchive_preview_note_path_from_file_path(self, file_path: str) -> str:
        return os.path.join(os.path.dirname(file_path), self.PAWCHIVE_PREVIEW_NOTE)

    def get_pawchive_preview_note_paths(self, creator: Creator, post: Post) -> set[str]:
        note_paths: set[str] = set()
        for attachment in post.attachments:
            template_variables = FileTemplateVaribales(creator, post, attachment)
            file_path = generate_file_path(
                self.path,
                self.output_templates.get("attachments", self.DEFAULT_OUTPUT_TEMPLATE),
                template_variables.toDict(self.custom_template_variables),
                self.restrict_names,
            )
            note_paths.add(self.get_pawchive_preview_note_path_from_file_path(file_path))
        return note_paths

    def get_pawchive_preview_note_path(self, creator: Creator, post: Post) -> str | None:
        if not post.attachments:
            return None
        return next(iter(self.get_pawchive_preview_note_paths(creator, post)))

    def get_pawchive_preview_urls(self, attachment: Attachment) -> list[str]:
        preview_path = attachment.preview_path or attachment.path
        preview_name = attachment.preview_name or attachment.name
        if not preview_path:
            return []

        urls = [
            f"{self.PAWCHIVE_FILE_DOMAIN}/thumbnail/data{preview_path}?f={preview_name}",
            f"{self.PAWCHIVE_FILE_DOMAIN}/thumbnail/data{preview_path}",
        ]

        if attachment.preview_server:
            urls.extend(
                [
                    f"{attachment.preview_server}/thumbnail/data{preview_path}?f={preview_name}",
                    f"{attachment.preview_server}/thumbnail/data{preview_path}",
                    f"{attachment.preview_server}/data{preview_path}?f={preview_name}",
                ]
            )

        # If the API preview path differs from the original path, it may also
        # be available through Pawchive's normal file route.
        if attachment.preview_path and attachment.preview_path != attachment.path:
            urls.append(f"{self.PAWCHIVE_FILE_DOMAIN}/data{preview_path}?f={preview_name}")

        # Preserve order while removing duplicates.
        return list(dict.fromkeys(urls))

    def get_pawchive_preview_url(self, attachment: Attachment) -> str | None:
        urls = self.get_pawchive_preview_urls(attachment)
        return urls[0] if urls else None

    def get_pawchive_webpage_preview_urls(self, webpage_url: str, attachment: Attachment) -> list[str]:
        try:
            response = self.session.get(webpage_url)
            response.raise_for_status()
            webpage = response.text
        except Exception as e:
            print(f"[Error] Failed to read post page for preview URLs from {webpage_url!r}: {e}")
            return []

        raw_urls = re.findall(r"""(?:src|href|data-src)=["']([^"']+)["']""", webpage, flags=re.IGNORECASE)
        raw_urls += re.findall(r"""url\(["']?([^"')]+)["']?\)""", webpage, flags=re.IGNORECASE)

        image_urls = []
        for raw_url in raw_urls:
            url = urljoin(webpage_url, raw_url)
            if not re.search(r"\.(?:jpe?g|png|gif|webp|avif)(?:\?|$)", url, flags=re.IGNORECASE):
                continue
            image_urls.append(url)

        image_urls = list(dict.fromkeys(image_urls))
        if not image_urls:
            return []

        identifiers = [identifier for identifier in (attachment.preview_name, attachment.name, attachment.preview_path, attachment.path) if identifier]
        matching_urls = [url for url in image_urls if any(identifier in url for identifier in identifiers)]
        if matching_urls:
            return matching_urls

        thumbnail_urls = [url for url in image_urls if "thumbnail" in url.lower() or "/data/" in url.lower()]
        if thumbnail_urls:
            return thumbnail_urls

        return image_urls

    def download_pawchive_preview(self, attachment: Attachment, file_path: str, original_url: str, webpage_url: str | None = None) -> bool:
        preview_urls = self.get_pawchive_preview_urls(attachment)
        if not preview_urls:
            print(f"[warning] No preview is available for attachment {attachment.name!r}")
            return False

        print(f"[warning] Original attachment returned HTTP 404. Trying {len(preview_urls)} preview URL(s).")
        for preview_url in preview_urls:
            try:
                print(f"[downloading] Preview source: {preview_url!r}")
                download_file(self.session, preview_url, file_path, temp_file=not self.no_tmp)
            except Exception as e:
                print(f"[Error] Failed to download preview from {preview_url!r}: {e}")
                continue

            note_path = self.get_pawchive_preview_note_path_from_file_path(file_path)
            try:
                self.write_pawchive_preview_note(note_path, attachment, original_url, preview_url)
            except Exception as e:
                print(f"[Error] Failed to write preview fallback note at {note_path!r}: {e}")
                return False

            print(f"[warning] Downloaded preview instead of original attachment {attachment.name!r}")
            return True

        # The API preview URLs can be stale. As a last resort inspect the post
        # page itself for thumbnail/image URLs.
        if webpage_url:
            webpage_urls = self.get_pawchive_webpage_preview_urls(webpage_url, attachment)
            if webpage_urls:
                print(f"[warning] Trying {len(webpage_urls)} preview URL(s) found on the post page.")
            for preview_url in webpage_urls:
                try:
                    print(f"[downloading] Preview source: {preview_url!r}")
                    download_file(self.session, preview_url, file_path, temp_file=not self.no_tmp)
                except Exception as e:
                    print(f"[Error] Failed to download preview from {preview_url!r}: {e}")
                    continue

                note_path = self.get_pawchive_preview_note_path_from_file_path(file_path)
                try:
                    self.write_pawchive_preview_note(note_path, attachment, original_url, preview_url)
                except Exception as e:
                    print(f"[Error] Failed to write preview fallback note at {note_path!r}: {e}")
                    return False

                print(f"[warning] Downloaded preview instead of original attachment {attachment.name!r}")
                return True

        print(f"[Error] All preview URLs failed for attachment {attachment.name!r}")
        return False

    def write_pawchive_preview_note(self, note_path: str, attachment: Attachment, original_url: str, preview_url: str) -> None:
        os.makedirs(os.path.dirname(note_path), exist_ok=True)

        existing = ""
        if os.path.exists(note_path):
            with open(note_path, "r", encoding="utf-8") as f:
                existing = f.read()

        if existing:
            content = existing
            if not content.endswith("\n"):
                content += "\n"
        else:
            content = (
                "One or more attachments were unavailable from Pawchive (HTTP 404).\n"
                "Preview images were downloaded instead. Re-run the downloader after\n"
                "Pawchive imports the original files; this note will be removed once\n"
                "all original attachments have been downloaded successfully.\n\n"
            )

        entry = f"- {attachment.name} ({attachment.path})\n  Original: {original_url}\n  Preview:  {preview_url}\n"
        if entry not in content:
            content += entry

        with open(note_path, "w", encoding="utf-8") as f:
            f.write(content)

    def write_post_json(self, creator: Creator, post: Post) -> None:
        print("[writing] Post Json")

        sha256 = compute_sha256(json.dumps(post.json))
        attachment = Attachment(name=f"{post.id}.json", path=f"{sha256}.json")
        template_variables = FileTemplateVaribales(creator, post, attachment)
        file_path = generate_file_path(
            self.path,
            self.output_templates.get("json", self.DEFAULT_OUTPUT_TEMPLATE),
            template_variables.toDict(self.custom_template_variables),
            self.restrict_names,
        )
        expected_sha256 = template_variables.sha256

        if os.path.exists(file_path):
            actual_sha256 = get_sha256_hash(file_path)

            if self.force_overwrite is False:
                print(f"[info] File already exists at {file_path}")
                if expected_sha256 != actual_sha256:
                    print(f'[warning] File sha256 mismatch. Expected "{expected_sha256}" recieved"{actual_sha256}"')
                return

            elif self.force_overwrite == "soft" and expected_sha256 == actual_sha256:
                print(f"[info] File already exists with matching sha256 at {file_path}")
                return

        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        print(f"[writing] Destination: {file_path!r}")

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(post.json, f, indent=4)

    def write_post_content(self, creator: Creator, post: Post) -> None:
        print("[writing] Post Content")

        sha256 = compute_sha256(post.content)
        attachment = Attachment(name="content.html", path=f"{sha256}.html")
        template_variables = FileTemplateVaribales(creator, post, attachment)
        file_path = generate_file_path(
            self.path,
            self.output_templates.get("content", self.DEFAULT_OUTPUT_TEMPLATE),
            template_variables.toDict(self.custom_template_variables),
            self.restrict_names,
        )
        expected_sha256 = template_variables.sha256

        if os.path.exists(file_path):
            actual_sha256 = get_sha256_hash(file_path)

            if self.force_overwrite is False:
                print(f"[info] File already exists at {file_path}")
                if expected_sha256 != actual_sha256:
                    print(f'[warning] File sha256 mismatch. Expected "{expected_sha256}" recieved"{actual_sha256}"')
                return

            elif self.force_overwrite == "soft" and expected_sha256 == actual_sha256:
                print(f"[info] File already exists with matching sha256 at {file_path}")
                return

        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        print(f"[writing] Destination: {file_path!r}")

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(post.content)

    def attachment_matches_filters(self, attachment) -> bool:
        skip_extensions = self.attachment_filters.get("skip_extensions", None)
        file_ext = os.path.splitext(attachment.name)[-1][1:]

        return bool(skip_extensions and file_ext in skip_extensions)

    def post_matches_filters(self, post: Post) -> bool:
        date_filter = self.post_filters.get("date", {})
        datebefore_filter = self.post_filters.get("datebefore", {})
        dateafter_filter = self.post_filters.get("dateafter", {})
        date_fields = ("added", "edited", "published")

        for field in date_fields:
            post_val = getattr(post, field)
            if (val := date_filter.get(field)) and post_val.date() != val.date():
                return True
            if (val := datebefore_filter.get(field)) and post_val.date() > val.date():
                return True
            if (val := dateafter_filter.get(field)) and post_val.date() < val.date():
                return True

        return False
