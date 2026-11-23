"""Build docs/STUDIO_API_SPEC.md: the hand-written overview, a tool reference generated
from the LIVE MCP tool schemas (so it can never drift from what is deployed), and the
voice API spec.

Run inside the web container so it reads the deployed code:
    docker compose exec -T web python /srv/docs/build_spec.py > docs/STUDIO_API_SPEC.md
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, "/srv")
from app import mcp  # noqa: E402

DOCS = Path(__file__).resolve().parent

GROUPS = [
    ("Video projects", ["list_projects", "get_project", "create_project", "update_project",
                        "add_media", "add_file", "get_upload_link", "plan_scenes", "set_scenes",
                        "render_video", "get_status", "cancel_render"]),
    ("Pictures", ["generate_image", "get_image", "list_images"]),
    ("Speech", ["generate_speech", "get_speech", "list_speech"]),
    ("Songs", ["generate_music", "get_music", "list_music"]),
    ("Personal voices", ["list_voices", "get_voice", "get_voice_upload_link", "train_voice",
                         "resing_song", "sing_song", "speak_as_voice", "get_voice_job"]),
]


def _type(p: dict) -> str:
    t = p.get("type", "")
    if t == "array":
        inner = p.get("items", {})
        t = f"array of {inner.get('type', 'object')}"
        if inner.get("enum"):
            t += f" ({', '.join(map(str, inner['enum']))})"
    if p.get("enum"):
        t += " — one of: " + ", ".join(f"`{e}`" for e in p["enum"])
    rng = []
    for k, sym in (("minimum", "≥"), ("maximum", "≤"), ("maxLength", "max length")):
        if k in p:
            rng.append(f"{sym} {p[k]}")
    if rng:
        t += " (" + ", ".join(rng) + ")"
    if t.startswith("object") and p.get("properties"):
        t += " {" + ", ".join(p["properties"]) + "}"
    return t


def _cell(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).replace("|", "\\|").strip()


def tool_reference() -> str:
    by = {t["spec"]["name"]: t["spec"] for t in mcp.TOOLS}
    out = ["## Part B — MCP tool reference (generated from the live server)\n",
           f"{len(by)} tools. Each one is `tools/call` with `{{\"name\": …, \"arguments\": {{…}}}}`. "
           "Arguments not listed are rejected (`additionalProperties: false`).\n"]
    seen = set()
    for group, names in GROUPS:
        out.append(f"### {group}\n")
        for n in names:
            if n not in by:
                continue
            seen.add(n)
            out.append(_tool(by[n]))
    rest = [n for n in by if n not in seen]
    if rest:
        out.append("### Other\n")
        out.extend(_tool(by[n]) for n in rest)
    return "\n".join(out)


def _tool(s: dict) -> str:
    ann = s.get("annotations", {})
    flags = ", ".join(x for x, on in (("read-only", ann.get("readOnlyHint")),
                                      ("destructive", ann.get("destructiveHint"))) if on)
    lines = [f"#### `{s['name']}` — {s['title']}" + (f" *({flags})*" if flags else ""), "",
             s["description"].strip(), ""]
    props = s["inputSchema"].get("properties", {})
    req = set(s["inputSchema"].get("required", []))
    if props:
        lines += ["| Argument | Type | Required | Notes |", "|---|---|---|---|"]
        for k, p in props.items():
            lines.append(f"| `{k}` | {_cell(_type(p))} | {'**yes**' if k in req else ''} | "
                         f"{_cell(p.get('description', ''))} |")
    else:
        lines.append("*No arguments.*")
    return "\n".join(lines) + "\n"


def voice_part() -> str:
    text = (DOCS / "VOICE_API_SPEC.md").read_text()
    body = text.split("\n---\n", 1)[1] if "\n---\n" in text else text
    # Demote headings one level so they nest under Part C.
    body = re.sub(r"^(#{1,5}) ", lambda m: "#" + m.group(1) + " ", body, flags=re.M)
    return ("## Part C — Personal voices: REST `/v1` API (layers 1–3)\n\n"
            "This is the full voice specification (source: `docs/VOICE_API_SPEC.md`). Its §-numbers are "
            "its own.\n\n" + body)


if __name__ == "__main__":
    print((DOCS / "spec" / "overview.md").read_text().rstrip())
    print("\n---\n")
    print(tool_reference().rstrip())
    print("\n---\n")
    print(voice_part().rstrip())
