import json, glob, os, re, collections

reads = {}          # tool_use_id -> file_path
best = {}           # file_path -> (len, text)
def note(fp, txt):
    if not fp or not isinstance(txt, str) or not txt.strip():
        return
    if len(txt) > len(best.get(fp, ("",))[0] if False else best.get(fp, "")):
        best[fp] = txt

for p in sorted(glob.glob("/root/.claude/**/*.jsonl", recursive=True)):
    try:
        fh = open(p, encoding="utf-8", errors="replace")
    except Exception:
        continue
    for line in fh:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        stack = [rec]
        while stack:
            o = stack.pop()
            if isinstance(o, dict):
                if o.get("type") == "tool_use":
                    inp = o.get("input") or {}
                    fp = inp.get("file_path") or inp.get("path") or ""
                    if isinstance(fp, str) and "conflict-monitor" in fp:
                        reads[o.get("id")] = fp
                        body = inp.get("content")
                        if isinstance(body, str):
                            note(fp, body)
                if o.get("type") == "tool_result":
                    tid = o.get("tool_use_id")
                    fp = reads.get(tid)
                    if fp:
                        c = o.get("content")
                        txt = ""
                        if isinstance(c, str):
                            txt = c
                        elif isinstance(c, list):
                            txt = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                        note(fp, txt)
                stack.extend(o.values())
            elif isinstance(o, list):
                stack.extend(o)

out = "/tmp/recovered"
os.makedirs(out, exist_ok=True)
rows = []
for fp, txt in best.items():
    # strip the "cat -n" style line-number prefixes Read adds
    lines = txt.split("\n")
    stripped = [re.sub(r"^\s*\d+\t", "", l) for l in lines]
    body = "\n".join(stripped)
    rel = fp.replace("/opt/conflict-monitor/", "").replace("/", "__").lstrip("_")
    if not rel:
        continue
    with open(os.path.join(out, rel), "w", encoding="utf-8") as f:
        f.write(body)
    rows.append((len(body), body.count("\n") + 1, fp))
rows.sort(reverse=True)
print("recovered %d files into %s\n" % (len(rows), out))
print("  bytes   lines  file")
for b, l, fp in rows[:40]:
    print("%7d  %6d  %s" % (b, l, fp))
