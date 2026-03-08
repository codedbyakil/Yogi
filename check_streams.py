import requests
import sys

source = sys.argv[1]
output = sys.argv[2]

timeout = 8

headers = {
    "User-Agent": "Mozilla/5.0",
    "Range": "bytes=0-1024"
}

with open(source, "r", encoding="utf-8", errors="ignore") as f:
    lines = f.readlines()

out = ["#EXTM3U\n"]

i = 0
while i < len(lines):
    if lines[i].startswith("#EXTINF"):
        info = lines[i]
        url = lines[i+1].strip()

        try:
            r = requests.get(url, headers=headers, timeout=timeout, stream=True)
            if r.status_code in [200, 206]:
                out.append(info)
                out.append(url + "\n")
        except:
            pass

        i += 2
    else:
        i += 1

with open(output, "w", encoding="utf-8") as f:
    f.writelines(out)
