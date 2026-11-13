"""
Modulo di Esportazione Note e Sbobine (Markdown, DOCX, PDF) per FlowLocal.
Supporta impaginazione accademica, formattazione grassetti inline ed evidenziazione
dei segnaposto [Audio non chiaro ...].
"""

import os
import re
import html
from typing import List, Dict, Union, Any

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QTextDocument, QPageLayout, QPageSize
from PyQt6.QtCore import QMarginsF
from PyQt6.QtPrintSupport import QPrinter

import docx
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn


AUDIO_PLACEHOLDER_REGEX = re.compile(
    r'(\*{0,2}\[Audio non chiaro[^\n\]]*\]\*{0,2})',
    re.IGNORECASE
)


def _normalize_notes(notes: Union[Dict[str, Any], List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if isinstance(notes, dict):
        return [notes]
    return list(notes)


def export_to_md(notes: Union[Dict[str, Any], List[Dict[str, Any]]], output_path: str):
    """Esporta note o sbobine in formato Markdown (.md)."""
    items = _normalize_notes(notes)
    if not items:
        raise ValueError("Nessuna nota fornita per l'esportazione.")

    parts = ["# FlowLocal — Note & Sbobine\n"]
    for n in items:
        title = n.get("title") or "Nota"
        parts.append(f"## {title}")

        meta_parts = []
        if n.get("created_at"):
            meta_parts.append(str(n.get("created_at")))
        if n.get("note_type"):
            meta_parts.append(str(n.get("note_type")))
        if n.get("tags"):
            meta_parts.append(str(n.get("tags")))

        if meta_parts:
            parts.append(f"*{' · '.join(meta_parts)}*\n")

        content = (n.get("content_markdown") or "").strip()
        parts.append(content + "\n")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


def _add_styled_runs_to_paragraph(paragraph, text: str, default_font="Calibri", default_size=10.5):
    """Scompone il testo per applicare grassetti inline (**...**) e stile badge ambra per [Audio non chiaro]."""
    parts = re.split(r'(\*\*.*?\*\*)', text)
    for part in parts:
        if not part:
            continue
        is_bold = False
        run_text = part
        if run_text.startswith("**") and run_text.endswith("**") and len(run_text) >= 4:
            is_bold = True
            run_text = run_text[2:-2]

        sub_parts = AUDIO_PLACEHOLDER_REGEX.split(run_text)
        for sp in sub_parts:
            if not sp:
                continue
            run = paragraph.add_run(sp)
            run.font.name = default_font
            run.font.size = Pt(default_size)

            clean_sp = sp.strip("*")
            if AUDIO_PLACEHOLDER_REGEX.match(clean_sp):
                run.bold = True
                run.font.color.rgb = RGBColor(180, 83, 9)  # Ambra / Rosso scuro
                try:
                    shd = OxmlElement('w:shd')
                    shd.set(qn('w:val'), 'clear')
                    shd.set(qn('w:color'), 'auto')
                    shd.set(qn('w:fill'), 'FEF3C7')  # Sfondo ambra leggero
                    run._r.get_or_add_rPr().append(shd)
                except Exception:
                    pass
            elif is_bold:
                run.bold = True
                run.font.color.rgb = RGBColor(15, 23, 42)  # Ardesia molto scuro
            else:
                run.font.color.rgb = RGBColor(51, 65, 85)


def export_to_docx(notes: Union[Dict[str, Any], List[Dict[str, Any]]], output_path: str):
    """Converte le note e sbobine in un documento Word (.docx) impaginato accademicamente."""
    items = _normalize_notes(notes)
    if not items:
        raise ValueError("Nessuna nota fornita per l'esportazione.")

    doc = docx.Document()

    # Impostazione margini A4 standard (1 pollice / 2.54 cm)
    for section in doc.sections:
        section.top_margin = Inches(1.0)
        section.bottom_margin = Inches(1.0)
        section.left_margin = Inches(1.0)
        section.right_margin = Inches(1.0)

    # Titolo Documento
    main_title_p = doc.add_paragraph()
    main_title_run = main_title_p.add_run("FlowLocal — Note & Sbobine")
    main_title_run.font.name = "Segoe UI"
    main_title_run.font.size = Pt(22)
    main_title_run.bold = True
    main_title_run.font.color.rgb = RGBColor(15, 23, 42)
    main_title_p.paragraph_format.space_after = Pt(12)

    for idx, n in enumerate(items):
        title = (n.get("title") or "Sbobina").strip()
        note_type = n.get("note_type") or "Nota"
        created_at = str(n.get("created_at") or "")
        tags = str(n.get("tags") or "")

        # Intestazione Nota
        h_p = doc.add_paragraph()
        h_run = h_p.add_run(title)
        h_run.font.name = "Segoe UI"
        h_run.font.size = Pt(16)
        h_run.bold = True
        h_run.font.color.rgb = RGBColor(30, 58, 138)  # Blu accademico profondo
        h_p.paragraph_format.space_before = Pt(16 if idx > 0 else 4)
        h_p.paragraph_format.space_after = Pt(2)

        # Metadati
        meta_items = [x for x in (created_at, note_type, tags) if x]
        if meta_items:
            m_p = doc.add_paragraph()
            m_run = m_p.add_run(" · ".join(meta_items))
            m_run.font.name = "Calibri"
            m_run.font.size = Pt(9.5)
            m_run.italic = True
            m_run.font.color.rgb = RGBColor(100, 116, 139)
            m_p.paragraph_format.space_after = Pt(8)

        # Contenuto Markdown riga per riga
        content = (n.get("content_markdown") or "").strip()
        lines = content.split("\n")

        for line in lines:
            line_str = line.strip()
            if not line_str:
                continue

            # Parsing a 4 livelli: ####, ###, ##, #
            if line_str.startswith("#### "):
                p = doc.add_paragraph()
                p.paragraph_format.space_before = Pt(8)
                p.paragraph_format.space_after = Pt(2)
                sub_text = re.sub(r'^#+\s*', '', line_str).strip()
                r = p.add_run(sub_text)
                r.font.name = "Segoe UI"
                r.font.size = Pt(12)
                r.bold = True
                r.font.color.rgb = RGBColor(30, 41, 59)  # Ardesia scuro

            elif line_str.startswith("### "):
                p = doc.add_paragraph()
                p.paragraph_format.space_before = Pt(10)
                p.paragraph_format.space_after = Pt(3)
                sub_text = re.sub(r'^#+\s*', '', line_str).strip()
                r = p.add_run(sub_text)
                r.font.name = "Segoe UI"
                r.font.size = Pt(13.5)
                r.bold = True
                r.font.color.rgb = RGBColor(30, 58, 138)  # Blu scuro #1E3A8A

            elif line_str.startswith("## "):
                p = doc.add_paragraph()
                p.paragraph_format.space_before = Pt(14)
                p.paragraph_format.space_after = Pt(4)
                sub_text = re.sub(r'^#+\s*', '', line_str).strip()
                r = p.add_run(sub_text)
                r.font.name = "Segoe UI"
                r.font.size = Pt(16)
                r.bold = True
                r.font.color.rgb = RGBColor(30, 58, 138)  # Blu scuro #1E3A8A

            elif line_str.startswith("# "):
                p = doc.add_paragraph()
                p.paragraph_format.space_before = Pt(16)
                p.paragraph_format.space_after = Pt(5)
                sub_text = re.sub(r'^#+\s*', '', line_str).strip()
                r = p.add_run(sub_text)
                r.font.name = "Segoe UI"
                r.font.size = Pt(20)
                r.bold = True
                r.font.color.rgb = RGBColor(15, 23, 42)  # Blu scuro #0F172A

            elif line_str.startswith("- ") or line_str.startswith("* "):
                p = doc.add_paragraph(style='List Bullet')
                p.paragraph_format.space_after = Pt(2)
                p.paragraph_format.line_spacing = 1.15
                bullet_content = line_str[2:].strip()
                _add_styled_runs_to_paragraph(p, bullet_content)

            elif re.match(r'^\d+\.\s+', line_str):
                p = doc.add_paragraph(style='List Number')
                p.paragraph_format.space_after = Pt(2)
                p.paragraph_format.line_spacing = 1.15
                num_content = re.sub(r'^\d+\.\s+', '', line_str)
                _add_styled_runs_to_paragraph(p, num_content)

            elif line_str.startswith("> "):
                p = doc.add_paragraph()
                p.paragraph_format.left_indent = Inches(0.25)
                p.paragraph_format.space_before = Pt(3)
                p.paragraph_format.space_after = Pt(3)
                p.paragraph_format.line_spacing = 1.2
                quote_text = line_str[2:].strip()
                _add_styled_runs_to_paragraph(p, quote_text, default_size=10.0)

            else:
                p = doc.add_paragraph()
                p.paragraph_format.space_after = Pt(6)
                p.paragraph_format.line_spacing = 1.25
                _add_styled_runs_to_paragraph(p, line_str)

        if idx < len(items) - 1:
            div_p = doc.add_paragraph()
            div_p.paragraph_format.space_before = Pt(14)
            div_p.paragraph_format.space_after = Pt(14)
            div_run = div_p.add_run("____________________________________________________")
            div_run.font.color.rgb = RGBColor(226, 232, 240)
            div_p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    doc.save(output_path)


STYLE_H1 = "font-size: 20pt; font-weight: bold; color: #0F172A; margin-top: 14pt; margin-bottom: 6pt;"
STYLE_H2 = "font-size: 15.5pt; font-weight: bold; color: #1E3A8A; margin-top: 14pt; margin-bottom: 5pt; border-bottom: 1px solid #CBD5E1; padding-bottom: 2pt;"
STYLE_H3 = "font-size: 13pt; font-weight: bold; color: #1D4ED8; margin-top: 10pt; margin-bottom: 4pt;"
STYLE_H4 = "font-size: 11.5pt; font-weight: bold; color: #334155; margin-top: 8pt; margin-bottom: 3pt;"
STYLE_BODY = "font-size: 10.5pt; line-height: 1.45; color: #1E293B;"
STYLE_P = "font-size: 10.5pt; line-height: 1.45; color: #1E293B; margin-top: 3pt; margin-bottom: 6pt;"
STYLE_LI = "font-size: 10.5pt; line-height: 1.45; color: #1E293B; margin-bottom: 3pt;"
STYLE_AUDIO_ALERT = "background-color: #FEF3C7; color: #92400E; font-weight: bold; padding: 2px 5px; border-radius: 3px;"


def _markdown_to_html_body(notes: List[Dict[str, Any]]) -> str:
    """Converte le note in HTML tipografico pulito per la renderizzazione PDF con stili inline espliciti."""
    html_parts = []
    html_parts.append(f"<h1 class='doc-title' style='{STYLE_H1} border-bottom: 2px solid #2563EB; padding-bottom: 4pt;'>FlowLocal — Note & Sbobine</h1>")

    def _replace_audio_alert(match):
        inner = match.group(1).strip("*")
        return f'<span class="audio-alert" style="{STYLE_AUDIO_ALERT}">{inner}</span>'

    for idx, n in enumerate(notes):
        title = html.escape((n.get("title") or "Sbobina").strip())
        note_type = html.escape(str(n.get("note_type") or "Nota"))
        created_at = html.escape(str(n.get("created_at") or ""))
        tags = html.escape(str(n.get("tags") or ""))

        html_parts.append("<div class='note-container'>")
        html_parts.append(f"<h2 class='note-title' style='{STYLE_H2}'>{title}</h2>")

        meta = " · ".join(x for x in (created_at, note_type, tags) if x)
        if meta:
            html_parts.append(f"<div class='meta' style='color: #64748B; font-size: 8.5pt; font-style: italic; margin-bottom: 8pt;'>{meta}</div>")

        content = (n.get("content_markdown") or "").strip()
        lines = content.split("\n")
        in_bullet_list = False
        in_number_list = False

        for line in lines:
            line_str = line.strip()
            if not line_str:
                if in_bullet_list:
                    html_parts.append("</ul>")
                    in_bullet_list = False
                if in_number_list:
                    html_parts.append("</ol>")
                    in_number_list = False
                continue

            # Sostituzioni inline: evidenziazione segnaposto audio e grassetti
            escaped = html.escape(line_str)
            escaped = re.sub(
                r'(\*{0,2}\[Audio non chiaro[^\n\]]*\]\*{0,2})',
                _replace_audio_alert,
                escaped,
                flags=re.IGNORECASE
            )
            # Converte grassetti Markdown **...** in <strong>...</strong>
            escaped = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', escaped)

            if line_str.startswith("#### "):
                if in_bullet_list:
                    html_parts.append("</ul>")
                    in_bullet_list = False
                if in_number_list:
                    html_parts.append("</ol>")
                    in_number_list = False
                header_text = re.sub(r'^#+\s*', '', escaped).strip()
                html_parts.append(f"<h4 style='{STYLE_H4}'>{header_text}</h4>")

            elif line_str.startswith("### "):
                if in_bullet_list:
                    html_parts.append("</ul>")
                    in_bullet_list = False
                if in_number_list:
                    html_parts.append("</ol>")
                    in_number_list = False
                header_text = re.sub(r'^#+\s*', '', escaped).strip()
                html_parts.append(f"<h3 style='{STYLE_H3}'>{header_text}</h3>")

            elif line_str.startswith("## "):
                if in_bullet_list:
                    html_parts.append("</ul>")
                    in_bullet_list = False
                if in_number_list:
                    html_parts.append("</ol>")
                    in_number_list = False
                header_text = re.sub(r'^#+\s*', '', escaped).strip()
                html_parts.append(f"<h2 style='{STYLE_H2}'>{header_text}</h2>")

            elif line_str.startswith("# "):
                if in_bullet_list:
                    html_parts.append("</ul>")
                    in_bullet_list = False
                if in_number_list:
                    html_parts.append("</ol>")
                    in_number_list = False
                header_text = re.sub(r'^#+\s*', '', escaped).strip()
                html_parts.append(f"<h1 style='{STYLE_H1}'>{header_text}</h1>")

            elif line_str.startswith("- ") or line_str.startswith("* "):
                if in_number_list:
                    html_parts.append("</ol>")
                    in_number_list = False
                if not in_bullet_list:
                    html_parts.append("<ul style='margin-top: 3pt; margin-bottom: 6pt; padding-left: 20px;'>")
                    in_bullet_list = True
                b_text = escaped[2:].strip()
                html_parts.append(f"<li style='{STYLE_LI}'>{b_text}</li>")

            elif re.match(r'^\d+\.\s+', line_str):
                if in_bullet_list:
                    html_parts.append("</ul>")
                    in_bullet_list = False
                if not in_number_list:
                    html_parts.append("<ol style='margin-top: 3pt; margin-bottom: 6pt; padding-left: 20px;'>")
                    in_number_list = True
                n_text = re.sub(r'^\d+\.\s+', '', escaped)
                html_parts.append(f"<li style='{STYLE_LI}'>{n_text}</li>")

            elif line_str.startswith("> "):
                if in_bullet_list:
                    html_parts.append("</ul>")
                    in_bullet_list = False
                if in_number_list:
                    html_parts.append("</ol>")
                    in_number_list = False
                quote_text = escaped[2:].strip()
                html_parts.append(
                    f"<div class='quote-box' style='background-color: #F8FAFC; border-left: 4px solid #2563EB; padding: 5pt 10pt; margin: 4pt 0; border-radius: 4px; font-size: 10pt; color: #334155;'>{quote_text}</div>"
                )

            else:
                if in_bullet_list:
                    html_parts.append("</ul>")
                    in_bullet_list = False
                if in_number_list:
                    html_parts.append("</ol>")
                    in_number_list = False
                html_parts.append(f"<p style='{STYLE_P}'>{escaped}</p>")

        if in_bullet_list:
            html_parts.append("</ul>")
        if in_number_list:
            html_parts.append("</ol>")

        html_parts.append("</div>")
        if idx < len(notes) - 1:
            html_parts.append("<hr class='divider' style='border: none; border-top: 1px solid #CBD5E1; margin: 16pt 0;' />")

    return "\n".join(html_parts)


def export_to_pdf(notes: Union[Dict[str, Any], List[Dict[str, Any]]], output_path: str):
    """Renderizza le note o sbobine in un PDF ad alta risoluzione tramite QTextDocument e QPrinter nativo Qt6."""
    items = _normalize_notes(notes)
    if not items:
        raise ValueError("Nessuna nota fornita per l'esportazione.")

    # Assicura che l'istanza QApplication sia attiva per le classi grafiche Qt
    app = QApplication.instance() or QApplication([])

    body_html = _markdown_to_html_body(items)

    css_stylesheet = f"""
    body {{
      font-family: 'Segoe UI', Arial, Helvetica, sans-serif;
      {STYLE_BODY}
      background-color: #FFFFFF;
    }}
    p {{
      {STYLE_P}
      text-align: justify;
    }}
    li {{
      {STYLE_LI}
    }}
    h1 {{
      {STYLE_H1}
    }}
    h2 {{
      {STYLE_H2}
    }}
    h3 {{
      {STYLE_H3}
    }}
    h4 {{
      {STYLE_H4}
    }}
    strong {{
      color: #0F172A;
      font-weight: bold;
    }}
    .audio-alert {{
      {STYLE_AUDIO_ALERT}
    }}
    .meta {{
      color: #64748B;
      font-size: 8.5pt;
      font-style: italic;
      margin-bottom: 8pt;
    }}
    .divider {{
      border: none;
      border-top: 1px solid #CBD5E1;
      margin: 16pt 0;
    }}
    """

    full_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
{css_stylesheet}
</style>
</head>
<body style="{STYLE_BODY}">
{body_html}
</body>
</html>"""

    doc = QTextDocument()
    doc.setDefaultStyleSheet(css_stylesheet)
    doc.setHtml(full_html)

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(output_path)
    printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    printer.setPageMargins(QMarginsF(18, 18, 18, 18), QPageLayout.Unit.Millimeter)

    doc.print(printer)


def _escape_latex_text(text: str) -> str:
    """Esegue l'escape dei caratteri speciali LaTeX nel testo normale (fuori da math/code)."""
    replacements = [
        ("\\", r"\textbackslash{}"),
        ("&", r"\&"),
        ("%", r"\%"),
        ("#", r"\#"),
        ("_", r"\_"),
        ("{", r"\{"),
        ("}", r"\}"),
        ("~", r"\textasciitilde{}"),
        ("^", r"\textasciicircum{}"),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def _convert_inline_md_to_tex(line: str) -> str:
    """Converte formattazione inline Markdown in LaTeX preservando $...$ e $$...$$."""
    tokens = re.split(r'(\$\$.*?\$\$|\$[^$\n]+?\$|`[^`\n]+?`)', line)
    out = []
    for tok in tokens:
        if not tok:
            continue
        if (tok.startswith("$$") and tok.endswith("$$")) or (tok.startswith("$") and tok.endswith("$")):
            out.append(tok)
        elif tok.startswith("`") and tok.endswith("`"):
            inner = _escape_latex_text(tok[1:-1])
            out.append(rf"\texttt{{{inner}}}")
        else:
            # Gestisci **bold** e *italic*
            parts = re.split(r'(\*\*.*?\*\*|\*[^*\n]+?\*)', tok)
            for p in parts:
                if not p:
                    continue
                if p.startswith("**") and p.endswith("**") and len(p) >= 4:
                    out.append(rf"\textbf{{{_escape_latex_text(p[2:-2])}}}")
                elif p.startswith("*") and p.endswith("*") and len(p) >= 2:
                    out.append(rf"\textit{{{_escape_latex_text(p[1:-1])}}}")
                else:
                    out.append(_escape_latex_text(p))
    return "".join(out)


def _markdown_to_tex_body(md_text: str) -> str:
    """Converte il corpo Markdown+LaTeX di una nota in codice LaTeX compilabile."""
    lines = (md_text or "").splitlines()
    out: List[str] = []
    i = 0
    in_itemize = False
    in_enumerate = False

    def _close_lists():
        nonlocal in_itemize, in_enumerate
        if in_itemize:
            out.append(r"\end{itemize}")
            in_itemize = False
        if in_enumerate:
            out.append(r"\end{enumerate}")
            in_enumerate = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Blocchi di codice / BibTeX fenced ```
        if stripped.startswith("```"):
            _close_lists()
            lang = stripped[3:].strip().lower()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            code_body = "\n".join(code_lines)
            if lang == "bibtex":
                out.append(r"\begin{verbatim}")
                out.append(code_body)
                out.append(r"\end{verbatim}")
            else:
                out.append(r"\begin{lstlisting}")
                out.append(code_body)
                out.append(r"\end{lstlisting}")
            i += 1
            continue

        # Blocco matematico $$ ... $$ multi-riga
        if stripped.startswith("$$") and not (stripped.endswith("$$") and len(stripped) > 4):
            _close_lists()
            math_lines = [line]
            i += 1
            while i < len(lines):
                math_lines.append(lines[i])
                if lines[i].strip().endswith("$$"):
                    break
                i += 1
            out.append("\n".join(math_lines))
            i += 1
            continue

        # Immagini ![alt](path)
        img_match = re.match(r'^!\[(.*?)\]\((.+?)\)\s*$', stripped)
        if img_match:
            _close_lists()
            alt = _escape_latex_text(img_match.group(1) or "Immagine")
            img_path = img_match.group(2).strip().replace("\\", "/")
            out.append(r"\begin{figure}[htbp]")
            out.append(r"\centering")
            out.append(rf"\includegraphics[width=0.85\linewidth]{{{img_path}}}")
            if alt:
                out.append(rf"\caption{{{alt}}}")
            out.append(r"\end{figure}")
            i += 1
            continue

        # Tabelle Markdown
        if stripped.startswith("|") and "|" in stripped[1:]:
            _close_lists()
            table_rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                row_s = lines[i].strip().strip("|")
                cells = [c.strip() for c in row_s.split("|")]
                # Salta la riga separatrice |---|---|
                if not all(re.match(r'^:?-{2,}:?$', c) for c in cells if c):
                    table_rows.append(cells)
                i += 1
            if table_rows:
                ncols = max(len(r) for r in table_rows)
                col_spec = " ".join(["l"] * ncols)
                out.append(r"\begin{table}[htbp]")
                out.append(r"\centering")
                out.append(rf"\begin{{tabular}}{{{col_spec}}}")
                out.append(r"\toprule")
                for r_idx, r in enumerate(table_rows):
                    padded = r + [""] * (ncols - len(r))
                    tex_cells = [_convert_inline_md_to_tex(c) for c in padded]
                    out.append(" & ".join(tex_cells) + r" \\")
                    if r_idx == 0 and len(table_rows) > 1:
                        out.append(r"\midrule")
                out.append(r"\bottomrule")
                out.append(r"\end{tabular}")
                out.append(r"\end{table}")
            continue

        # Titoli
        if stripped.startswith("### "):
            _close_lists()
            out.append(rf"\subsubsection{{{_convert_inline_md_to_tex(stripped[4:].strip())}}}")
        elif stripped.startswith("## "):
            _close_lists()
            out.append(rf"\subsection{{{_convert_inline_md_to_tex(stripped[3:].strip())}}}")
        elif stripped.startswith("# "):
            _close_lists()
            out.append(rf"\section{{{_convert_inline_md_to_tex(stripped[2:].strip())}}}")
        elif stripped.startswith("> "):
            _close_lists()
            out.append(r"\begin{quote}")
            out.append(_convert_inline_md_to_tex(stripped[2:].strip()))
            out.append(r"\end{quote}")
        elif re.match(r'^[-*]\s+', stripped):
            if in_enumerate:
                out.append(r"\end{enumerate}")
                in_enumerate = False
            if not in_itemize:
                out.append(r"\begin{itemize}")
                in_itemize = True
            item_txt = re.sub(r'^[-*]\s+', '', stripped)
            out.append(rf"  \item {_convert_inline_md_to_tex(item_txt)}")
        elif re.match(r'^\d+\.\s+', stripped):
            if in_itemize:
                out.append(r"\end{itemize}")
                in_itemize = False
            if not in_enumerate:
                out.append(r"\begin{enumerate}")
                in_enumerate = True
            item_txt = re.sub(r'^\d+\.\s+', '', stripped)
            out.append(rf"  \item {_convert_inline_md_to_tex(item_txt)}")
        elif not stripped:
            _close_lists()
            out.append("")
        else:
            _close_lists()
            out.append(_convert_inline_md_to_tex(line))

        i += 1

    _close_lists()
    return "\n".join(out)


def export_to_tex(notes: Union[Dict[str, Any], List[Dict[str, Any]]], output_path: str):
    """Esporta una o più note in formato LaTeX (.tex) completo di preambolo standard."""
    items = _normalize_notes(notes)
    if not items:
        raise ValueError("Nessuna nota fornita per l'esportazione.")

    first_title = _escape_latex_text((items[0].get("title") or "Documento FlowLocal").strip())
    if len(items) > 1:
        first_title = "FlowLocal — Note \\& Sbobine"
    author = _escape_latex_text((items[0].get("profile") or "FlowLocal").strip())

    parts = [
        r"\documentclass[11pt,a4paper]{article}",
        r"\usepackage[utf8]{inputenc}",
        r"\usepackage[T1]{fontenc}",
        r"\usepackage[italian]{babel}",
        r"\usepackage{amsmath, amssymb, graphicx}",
        r"\usepackage{booktabs}",
        r"\usepackage{listings}",
        r"\usepackage{hyperref}",
        "",
        rf"\title{{{first_title}}}",
        rf"\author{{{author}}}",
        r"\date{\today}",
        "",
        r"\begin{document}",
        r"\maketitle",
        "",
    ]

    for idx, n in enumerate(items):
        if len(items) > 1:
            t = _escape_latex_text((n.get("title") or f"Nota {idx + 1}").strip())
            parts.append(rf"\section*{{{t}}}")
        meta_bits = [b for b in (n.get("subject"), n.get("topic"), n.get("note_type")) if b]
        if meta_bits:
            parts.append(rf"\noindent\textit{{{_escape_latex_text(' · '.join(meta_bits))}}}\par\medskip")
        parts.append(_markdown_to_tex_body(n.get("content_markdown") or ""))
        if idx < len(items) - 1:
            parts.append(r"\clearpage")

    parts.append("")
    parts.append(r"\end{document}")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


