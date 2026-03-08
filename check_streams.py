import requests
import sys

source = sys.argv[1]
output = sys.argv[2]

timeout = 10

headers = {
    "User-Agent": "Mozilla/5.0",
    "Range": "bytes=0-1024"
}

with open(source, "r", encoding="utf-8", errors="ignore") as f:
    lines = f.readlines()

out = ["#EXTM3U\n"]

current_block = []
current_url = None

def check(url):
    try:
        r = requests.get(
            url,
            headers=headers,
            timeout=timeout,
            stream=True,
            allow_redirects=True
        )
        return r.status_code in [200,206]
    except:
        return None

for line in lines:

    if line.startswith("#EXTINF"):
        if current_block:
            if current_url:
                result = check(current_url)

                if result is True or result is None:
                    out.extend(current_block)

        current_block = [line]
        current_url = None

    elif line.strip().startswith("http"):
        current_url = line.strip()
        current_block.append(line)

    elif current_block:
        current_block.append(line)

if current_block:
    if current_url:
        result = check(current_url)
        if result is True or result is None:
            out.extend(current_block)

with open(output, "w", encoding="utf-8") as f:
    f.writelines(out)
