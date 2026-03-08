import sys

source = sys.argv[1]
output = sys.argv[2]

with open(source, "r", encoding="utf-8", errors="ignore") as f:
    lines = f.readlines()

out = ["#EXTM3U\n"]
block = []

for line in lines:
    if line.startswith("#EXTINF"):
        if block:
            out.extend(block)
            block = []
        block.append(line)

    elif block:
        block.append(line)

# add last block
if block:
    out.extend(block)

with open(output, "w", encoding="utf-8") as f:
    f.writelines(out)
