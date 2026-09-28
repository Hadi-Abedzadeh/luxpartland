import requests
import os
import time
from urllib.parse import urlparse


BASE_URL = "https://japanstor.com/wp-json/wc/store/v1/products"

DOWNLOAD_DIR = "japanstor_images"


TARGET_MONTHS = [
    "/2025/08/",
    "/2025/09/",
    "/2025/10/",
    "/2025/11/",
    "/2025/12/"
]


os.makedirs(
    DOWNLOAD_DIR,
    exist_ok=True
)


session = requests.Session()

session.headers.update({
    "User-Agent": "Mozilla/5.0"
})



def download_image(url):

    filename = os.path.basename(
        urlparse(url).path
    )


    if not filename:
        return


    filepath = os.path.join(
        DOWNLOAD_DIR,
        filename
    )


    # جلوگیری از دانلود تکراری
    if os.path.exists(filepath):

        print(
            "Exists:",
            filename
        )

        return


    try:

        r = session.get(
            url,
            timeout=60
        )


        if r.status_code == 200:

            with open(
                filepath,
                "wb"
            ) as f:

                f.write(
                    r.content
                )


            print(
                "Downloaded:",
                filename
            )


        else:

            print(
                "Failed:",
                r.status_code,
                url
            )


    except Exception as e:

        print(
            "ERROR:",
            e
        )




def get_products(page):

    r = session.get(
        BASE_URL,
        params={
            "per_page":100,
            "page":page
        },
        timeout=60
    )


    print(
        "Page:",
        page,
        "Status:",
        r.status_code
    )


    if "application/json" not in r.headers.get(
        "Content-Type",
        ""
    ):

        print(
            r.text[:300]
        )

        return []


    return r.json()



# گرفتن تعداد صفحات

first = session.get(
    BASE_URL,
    params={
        "per_page":100,
        "page":1
    }
)


total_pages = int(
    first.headers.get(
        "X-WP-TotalPages",
        1
    )
)


print(
    "Total pages:",
    total_pages
)



for page in range(
    1,
    total_pages + 1
):

    products = get_products(page)


    if not products:
        continue



    for product in products:


        for img in product.get(
            "images",
            []
        ):


            urls = []


            # تصویر اصلی
            if img.get("src"):

                urls.append(
                    img["src"]
                )


            # تمام سایزهای srcset
            srcset = img.get(
                "srcset",
                ""
            )


            if srcset:

                for item in srcset.split(","):

                    url = item.strip().split(" ")[0]

                    if url:

                        urls.append(url)



            # حذف تکراری‌ها
            urls = list(
                set(urls)
            )



            for url in urls:


                path = urlparse(url).path


                if any(
                    month in path
                    for month in TARGET_MONTHS
                ):


                    print(
                        "\nFOUND:",
                        url
                    )


                    download_image(
                        url
                    )


    # تا سایت بلاک نکند
    time.sleep(2)



print(
    "\nFINISHED"
)
