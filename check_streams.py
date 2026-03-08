import requests
import sys

source = sys.argv[1]
output = sys.argv[2]

timeout = 10

headers = {
    "User-Agent": "Mozilla/5.0",
    "Range": "bytes=0-1024"
}

def check(url):
    try:
        r = requests.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=True)

        # clearly dead streams
        if r.status_code in [404, 410]:
            return False

        # working
        if r.status_code in [200, 206]:
            return True

        # protected / blocked streams → keep
        return True

    except:
        # connection failure → remove
        return False


with open(source, "r", encoding="utf-8", errors="ignore") as f:
    lines = f.readlines()

out = ["#EXTM3U\n"]

block = []
url = None

for line in lines:

    if line.startswith("#EXTINF"):
        if block and url:
            if check(url):
                out.extend(block)

        block = [line]
        url = None

    elif line.strip().startswith("http"):
        url = line.strip()
        block.append(line)

    elif block:
        block.append(line)

# last entry
if block and url:
    if check(url):
        out.extend(block)

with open(output, "w", encoding="utf-8") as f:
    f.writelines(out)
