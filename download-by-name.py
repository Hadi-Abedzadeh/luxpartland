"""Download keyword-filtered WooCommerce images. Requires Python 3.9+ and requests.

Usage:
    python download_filtered_images.py
    python download_filtered_images.py "شمع"

Preserves YEAR/domain folders, original filenames, and upload-month filtering.
"""

import html
import os
import re
import unicodedata
import sys
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ==================== تنظیمات قابل تغییر ====================
# دامنه را بدون مسیر وارد کنید؛ https:// و www. هم قابل استفاده هستند.
DOMAIN = "chinestor.com"

# خالی باشد: کلمه از آرگومان خط فرمان یا ورودی هنگام اجرا گرفته می‌شود.
SEARCH_WORD = "شمع"

# True: واژه باید در نام محصول هم باشد؛ False: تمام نتایج جست‌وجوی API.
MATCH_NAME_ONLY = True

# ترتیب ثابت محصولات: جدیدترین محصولات ابتدا؛ تصاویر به ترتیب API.
ORDER_BY = "date"
ORDER = "desc"

# سال میلادی پوشه‌های آپلود وردپرس
YEAR = 2020

# ماه‌های موردنظر؛ می‌توانید ماه‌های غیرمتوالی هم انتخاب کنید.
# مثال: [1, 3, 12] یا برای تمام سال: list(range(1, 13))
MONTHS = [1,2,3,4,5,6,7,8,9,10,11,12]

API_WORKERS = 4
DOWNLOAD_WORKERS = 16
MAX_PENDING_DOWNLOADS = DOWNLOAD_WORKERS * 4
PER_PAGE = 100
TIMEOUT = (10, 60)  # connect timeout, read timeout
CHUNK_SIZE = 256 * 1024
LOG_EVERY = 50


# ==================== تنظیمات خودکار ====================
# این مسیر برای تمام دامنه‌ها ثابت می‌ماند.
API_PATH = "/wp-json/wc/store/v1/products"


def build_settings(domain, year, months):
    if not isinstance(domain, str) or not domain.strip():
        raise ValueError("DOMAIN must be a non-empty domain name")
    site_url = domain.strip().rstrip("/")
    if "://" not in site_url:
        site_url = "https://" + site_url
    parsed = urlsplit(site_url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or any(char.isspace() for char in parsed.netloc)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("DOMAIN must be a domain or HTTP(S) site URL without a path")
    # Validate an optional port before constructing the API URL.
    parsed.port

    if type(year) is not int or not 1 <= year <= 9999:
        raise ValueError("YEAR must be a Gregorian year as an integer")
    if not isinstance(months, (list, tuple, range)) or not months:
        raise ValueError("MONTHS must be a non-empty list, tuple or range")
    if any(type(month) is not int or not 1 <= month <= 12 for month in months):
        raise ValueError("Every month in MONTHS must be an integer from 1 to 12")

    # YEAR=2025, DOMAIN=bmwstor.com -> 2025/bmwstor.com
    site_domain = parsed.hostname.removeprefix("www.").rstrip(".")
    if not site_domain or any(char in site_domain for char in '/\\:*?"<>|'):
        raise ValueError("DOMAIN cannot produce a valid directory name")

    base_url = f"{parsed.scheme}://{parsed.netloc}{API_PATH}"
    download_dir = Path(str(year)) / site_domain
    target_months = tuple(f"/{year:04d}/{month:02d}/" for month in sorted(set(months)))
    return base_url, download_dir, target_months


BASE_URL, DOWNLOAD_DIR, TARGET_MONTHS = build_settings(DOMAIN, YEAR, MONTHS)

_local = threading.local()
_sessions = []
_sessions_lock = threading.Lock()


def get_session():
    # Each worker owns a Session and reuses its HTTP connections.
    if not hasattr(_local, "session"):
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})
        retries = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            max_retries=retries,
            pool_connections=4,
            pool_maxsize=1,
            pool_block=True,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _local.session = session
        with _sessions_lock:
            _sessions.append(session)
    return _local.session


def close_sessions():
    # Called only after all worker pools have stopped.
    for session in _sessions:
        session.close()
    _sessions.clear()
    if hasattr(_local, "session"):
        del _local.session


def normalize_text(value):
    """Normalize common Persian/Arabic spelling differences for name matching."""
    value = unicodedata.normalize("NFKC", html.unescape(str(value)))
    value = value.translate(str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "ـ": ""}))
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.replace("\u200c", " ").replace("\u200d", "")
    return re.sub(r"\s+", " ", value).strip().casefold()


def matches_product(product, keyword):
    if not isinstance(product, dict):
        return False
    return not MATCH_NAME_ONLY or normalize_text(keyword) in normalize_text(product.get("name", ""))


def get_products(page, keyword):
    with get_session().get(
        BASE_URL,
        params={
            "per_page": PER_PAGE,
            "page": page,
            "search": keyword,
            "orderby": ORDER_BY,
            "order": ORDER,
        },
        timeout=TIMEOUT,
    ) as response:
        response.raise_for_status()
        if "json" not in response.headers.get("Content-Type", "").lower():
            raise ValueError(f"Page {page}: server did not return JSON")
        products = response.json()
        if not isinstance(products, list):
            raise ValueError(f"Page {page}: expected a product list")
        return products, response.headers.get("X-WP-TotalPages")


def iter_pages(first_products, total_pages, keyword):
    # Fetch concurrently, but consume pages in API order with a bounded queue.
    yield 1, first_products, None
    with ThreadPoolExecutor(max_workers=API_WORKERS) as pool:
        pending = {}
        next_page = 2

        def fill_queue():
            nonlocal next_page
            while next_page <= total_pages and len(pending) < API_WORKERS:
                pending[next_page] = pool.submit(get_products, next_page, keyword)
                next_page += 1

        fill_queue()
        for page in range(2, total_pages + 1):
            future = pending.pop(page)
            try:
                products, _ = future.result()
                error = None
            except (requests.RequestException, ValueError) as exc:
                products, error = [], str(exc)
            fill_queue()
            yield page, products, error


def iter_images(products):
    for product in products:
        if not isinstance(product, dict):
            continue
        for image in product.get("images", []) or []:
            if not isinstance(image, dict):
                continue
            urls = []
            if image.get("src"):
                urls.append(image["src"])
            for item in (image.get("srcset") or "").split(","):
                parts = item.split()
                if parts:
                    urls.append(parts[0])

            for url in urls:
                if not isinstance(url, str):
                    continue
                parsed = urlsplit(url)
                if parsed.scheme not in ("http", "https") or not parsed.netloc:
                    continue
                if not any(month in parsed.path for month in TARGET_MONTHS):
                    continue
                filename = os.path.basename(parsed.path)
                if filename and filename not in (".", ".."):
                    yield url, filename


def download_image(url, filepath):
    # A final filename appears only after the entire response was saved.
    part = filepath.with_name(filepath.name + ".part")
    try:
        if filepath.is_file() and filepath.stat().st_size > 0:
            return "skipped", 0, ""

        with get_session().get(url, timeout=TIMEOUT, stream=True) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError(f"Unexpected HTTP status: {response.status_code}")
            if "text/html" in response.headers.get("Content-Type", "").lower():
                raise ValueError("Server returned HTML instead of an image")

            size = 0
            with part.open("wb") as output:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        output.write(chunk)
                        size += len(chunk)

            if size == 0:
                raise ValueError("Empty image response")
            # iter_content decodes compression; compare only uncompressed bodies.
            length = response.headers.get("Content-Length")
            encoding = response.headers.get("Content-Encoding", "identity").lower()
            if length is not None and encoding in ("", "identity"):
                if size != int(length):
                    raise ValueError(f"Incomplete image: {size}/{length} bytes")

        os.replace(part, filepath)
        return "downloaded", size, ""
    except (requests.RequestException, OSError, ValueError) as exc:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        return "failed", 0, str(exc)


def main():
    keyword = " ".join(sys.argv[1:]).strip() or SEARCH_WORD.strip()
    if not keyword:
        keyword = input("کلمهٔ جست‌وجو را وارد کنید: ").strip()
    if not normalize_text(keyword):
        print("ERROR: Search word must not be empty.", flush=True)
        return 1
    print(f"Search: {keyword} | Name only: {MATCH_NAME_ONLY}", flush=True)
    print(f"Output: {DOWNLOAD_DIR.resolve()} | Upload months: {MONTHS}", flush=True)
    started = time.perf_counter()
    stats = Counter()
    seen_names = set()
    pending = {}
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # Keep the original flat folder and filenames. As in the original script,
    # files with the same name share a destination; schedule that name only once.
    with os.scandir(DOWNLOAD_DIR) as entries:
        existing = {
            os.path.normcase(entry.name)
            for entry in entries
            if entry.is_file() and entry.stat().st_size > 0
        }

    def collect(futures):
        for future in futures:
            url = pending.pop(future)
            status, size, error = future.result()
            stats[status] += 1
            stats["bytes"] += size
            stats["completed"] += 1
            if error:
                print(f"FAILED: {url} | {error}", flush=True)
            if stats["completed"] % LOG_EVERY == 0:
                print(
                    f"Downloaded: {stats['downloaded']} | "
                    f"Skipped: {stats['skipped']} | Failed: {stats['failed']}",
                    flush=True,
                )

    try:
        first_products, page_count = get_products(1, keyword)
        if page_count is None:
            raise ValueError("X-WP-TotalPages is missing; cannot discover all pages")
        total_pages = max(1, int(page_count))
        print(f"Total pages: {total_pages}", flush=True)

        with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as downloads:
            for page, products, error in iter_pages(first_products, total_pages, keyword):
                collect([future for future in pending if future.done()])
                if error:
                    stats["pages_failed"] += 1
                    print(f"PAGE FAILED: {page} | {error}", flush=True)
                    continue

                stats["products_checked"] += len(products)
                products = [product for product in products if matches_product(product, keyword)]
                stats["products_matched"] += len(products)
                stats["pages_ok"] += 1
                print(
                    f"Page {page}: {len(products)} matching products | "
                    f"Processed: {stats['pages_ok']}/{total_pages}",
                    flush=True,
                )
                for url, filename in iter_images(products):
                    key = os.path.normcase(filename)
                    if key in seen_names:
                        continue
                    seen_names.add(key)
                    if key in existing:
                        stats["skipped"] += 1
                        continue

                    # Bound queued work instead of submitting every image at once.
                    if len(pending) >= MAX_PENDING_DOWNLOADS:
                        done, _ = wait(pending, return_when=FIRST_COMPLETED)
                        collect(done)
                    future = downloads.submit(
                        download_image, url, DOWNLOAD_DIR / filename
                    )
                    pending[future] = url

            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                collect(done)
    except (requests.RequestException, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1
    finally:
        close_sessions()

    elapsed = time.perf_counter() - started
    print(
        f"\nFINISHED in {elapsed:.1f}s | "
        f"Matched products: {stats['products_matched']} | "
        f"Downloaded: {stats['downloaded']} | Skipped: {stats['skipped']} | "
        f"Failed images: {stats['failed']} | Failed pages: {stats['pages_failed']} | "
        f"Data: {stats['bytes'] / 1024 / 1024:.1f} MB",
        flush=True,
    )
    return 1 if stats["failed"] or stats["pages_failed"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Run again to continue; completed files are skipped.")
        sys.exit(130)
