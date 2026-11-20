from pathlib import Path
import re


def render_html(report_json: str) -> str:
    directory = Path(__file__).with_name("templates")
    template = (directory / "spark_report.html").read_text(encoding="utf-8")
    html, count = re.subn(
        r"const reportData = [\s\S]*?\n\n    const state =",
        lambda _: "const reportData = " + report_json + ";\n\n    const state =",
        template, count=1,
    )
    if count != 1:
        raise RuntimeError("未能在报告模板中替换 reportData")
    for name in ("workspace.css", "lucide.min.js", "workspace.js"):
        marker = f"<!-- report-resource:{name} -->"
        if html.count(marker) != 1:
            raise RuntimeError(f"报告模板资源标记无效: {name}")
        content = (directory / name).read_text(encoding="utf-8-sig")
        if name == "lucide.min.js":
            license_text = (directory / "lucide.LICENSE").read_text(encoding="utf-8")
            content = f"/*\n{license_text}\n*/\n{content}"
        tag = "style" if name.endswith(".css") else "script"
        content = content.replace(f"</{tag}", f"<\\/{tag}")
        html = html.replace(marker, f"<{tag}>\n{content}\n</{tag}>")
    return html
