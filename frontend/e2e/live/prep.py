# prep.py <intro|all|p5>: prepares the live-check repo through the API on 127.0.0.1:8765.
import json, sys, urllib.request
B="http://127.0.0.1:8765"; mode=sys.argv[1]
def get(p): return json.load(urllib.request.urlopen(B+p))
def post(p,d):
    r=urllib.request.Request(B+p,data=json.dumps(d).encode(),headers={"Content-Type":"application/json"},method="POST"); return json.load(urllib.request.urlopen(r))
def adopt(headings):
    for s in get("/api/spec/skeleton")["sections"]:
        if headings is None or s["heading"] in headings: post("/api/spec/adopt",{"kind":"section","file":s["file"],"line":s["line"]})
    while True:
        todo=[p for p in get("/api/spec/skeleton")["paragraphs"] if p["section_heading"] and (headings is None or p["section_heading"] in headings) and not p["marker_id"]]
        if not todo: break
        p=todo[0]; post("/api/spec/adopt",{"kind":"paragraph","file":p["file"],"line_start":p["line_start"]})
adopt(None if mode=="all" else ["Introduction"])
if mode=="p5":
    post("/api/git/commit",{"message":"Adopt introduction"}); post("/api/snapshots",{"note":"v1"})
    f=get("/api/file?path=sections/intro.tex"); c=f["content"].replace("Snapshot restore is the main alternative.","Restoring from a snapshot is the usual alternative to warm pools.")
    r=urllib.request.Request(B+"/api/file",data=json.dumps({"path":"sections/intro.tex","content":c,"base_sha256":f.get("sha256")}).encode(),headers={"Content-Type":"application/json"},method="PUT"); urllib.request.urlopen(r)
print("prepared", mode)
