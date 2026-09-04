from pathlib import Path
from urllib.parse import unquote, urlparse

import markdown
from loguru import logger
from xhtml2pdf import pisa


_REPORT_CSS = """
@page {
    size: A4;
    margin: 18mm 16mm 20mm 16mm;
}

body {
    color: #1f2937;
    font-family: STSong-Light;
    font-size: 10.5pt;
    line-height: 1.6;
}

h1, h2, h3, h4 {
    color: #111827;
    margin-bottom: 8pt;
    -pdf-keep-with-next: true;
}

h1 { font-size: 22pt; }
h2 { font-size: 17pt; }
h3 { font-size: 14pt; }

p { margin: 5pt 0 8pt 0; }

table {
    margin: 8pt 0 12pt 0;
    width: 100%;
}

th, td {
    border: 0.6pt solid #9ca3af;
    padding: 5pt;
    vertical-align: top;
}

th {
    background-color: #e5e7eb;
    color: #111827;
    font-weight: bold;
}

pre {
    background-color: #f3f4f6;
    border: 0.6pt solid #d1d5db;
    font-family: STSong-Light;
    font-size: 8.5pt;
    padding: 7pt;
    white-space: pre-wrap;
}

code {
    font-family: STSong-Light;
    font-size: 9pt;
}

blockquote {
    border-left: 3pt solid #9ca3af;
    color: #4b5563;
    margin-left: 4pt;
    padding-left: 9pt;
}

a { color: #2563eb; }
"""


def _resolve_resource_uri(uri: str, base_dir: Path) -> str:
    """Only allow embedded data or local resources inside the report directory."""
    if uri.startswith("data:"):
        return uri

    parsed = urlparse(uri)
    if parsed.scheme or parsed.netloc:
        raise ValueError(f"PDF 中禁止加载外部资源: {uri}")

    resource_path = Path(unquote(parsed.path))
    candidate = (
        resource_path.resolve()
        if resource_path.is_absolute()
        else (base_dir / resource_path).resolve()
    )
    try:
        candidate.relative_to(base_dir)
    except ValueError as exc:
        raise ValueError(f"PDF 资源超出当前会话目录: {uri}") from exc
    return str(candidate)


def convert_md_to_pdf_via_html(md_abs_path: Path, pdf_abs_path: Path) -> str:
    """Render a Markdown file to PDF without relying on Microsoft Word."""
    md_abs_path = md_abs_path.resolve()
    pdf_abs_path = pdf_abs_path.resolve()
    base_dir = md_abs_path.parent

    try:
        md_content = md_abs_path.read_text(encoding="utf-8")
        html_body = markdown.markdown(
            md_content,
            extensions=["tables", "fenced_code"],
        )
        html_content = f"""
        <html>
        <head>
            <meta charset="UTF-8">
            <style>{_REPORT_CSS}</style>
        </head>
        <body>{html_body}</body>
        </html>
        """

        pdf_abs_path.parent.mkdir(parents=True, exist_ok=True)
        with pdf_abs_path.open("wb") as output:
            result = pisa.CreatePDF(
                src=html_content,
                dest=output,
                path=str(base_dir),
                encoding="utf-8",
                link_callback=lambda uri, _relative_uri: _resolve_resource_uri(
                    uri, base_dir
                ),
                raise_exception=True,
            )

        if result.err or not pdf_abs_path.exists() or pdf_abs_path.stat().st_size == 0:
            pdf_abs_path.unlink(missing_ok=True)
            return "转换失败：PDF 渲染引擎未生成有效文件"

        return f"成功转换: {pdf_abs_path} (xhtml2pdf引擎)"
    except Exception as exc:
        pdf_abs_path.unlink(missing_ok=True)
        logger.error(f"HTML转换PDF失败: {exc}", exc_info=True)
        return f"转换失败: {exc}"
