#!/usr/bin/env python3
"""
Convert Markdown files to DOCX format.
Creates robust, Microsoft Word-compatible DOCX documents using Python standard library only.
"""

import argparse
import os
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
import uuid


# XML namespaces for DOCX
NAMESPACES = {
    'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
    'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
    'cp': 'http://schemas.openxmlformats.org/package/2006/metadata/core-properties',
    'dc': 'http://purl.org/dc/elements/1.1/',
    'dcterms': 'http://purl.org/dc/terms/',
    'dcmitype': 'http://purl.org/dc/dcmitype/',
    'xsi': 'http://www.w3.org/2001/XMLSchema-instance',
    'vt': 'http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes',
    'ep': 'http://schemas.openxmlformats.org/officeDocument/2006/extended-properties',
    'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
    'wp': 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing',
}


def validate_path(path_str, description):
    """
    Validate that a path is workspace-relative and safe.
    Rejects absolute paths and path traversal attempts.
    """
    if not path_str:
        raise ValueError(f"{description} cannot be empty")

    # Reject absolute paths
    if os.path.isabs(path_str):
        raise ValueError(f"{description} must be workspace-relative, not absolute: {path_str}")

    # Reject path traversal
    if '..' in path_str.split(os.sep):
        raise ValueError(f"{description} cannot contain '..' for path traversal: {path_str}")

    # Check for .. in any form (including on Windows)
    normalized = os.path.normpath(path_str)
    if '..' in normalized.split(os.sep):
        raise ValueError(f"{description} cannot contain '..' for path traversal: {path_str}")

    return path_str


def validate_extensions(input_path, output_path):
    """Validate that input and output files have correct extensions."""
    input_ext = Path(input_path).suffix.lower()
    output_ext = Path(output_path).suffix.lower()

    valid_input_exts = ['.md', '.markdown']
    if input_ext not in valid_input_exts:
        raise ValueError(f"Input file must have .md or .markdown extension, got: {input_ext}")

    if output_ext != '.docx':
        raise ValueError(f"Output file must have .docx extension, got: {output_ext}")


def workspace_path(path_str):
    """Resolve a validated workspace-relative path against the workspace root."""
    workspace_root = Path(__file__).resolve().parents[2]
    return workspace_root / path_str


def read_markdown(input_path):
    """Read Markdown content from file with UTF-8 encoding."""
    try:
        with open(workspace_path(input_path), 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        raise ValueError(f"Input file not found: {input_path}")
    except PermissionError:
        raise ValueError(f"Permission denied reading file: {input_path}")
    except UnicodeDecodeError:
        raise ValueError(f"File is not valid UTF-8 text: {input_path}")
    except Exception as e:
        raise ValueError(f"Error reading file: {e}")


class MarkdownParser:
    """Parse Markdown into structured elements for DOCX conversion."""

    def __init__(self, content):
        self.content = content
        self.elements = []

    def parse(self):
        """Parse Markdown content into structured elements."""
        lines = self.content.split('\n')
        i = 0
        in_code_block = False
        code_fence_char = None
        code_lines = []

        while i < len(lines):
            line = lines[i]

            # Handle code blocks
            if line.startswith('```'):
                if not in_code_block:
                    # Start code block
                    in_code_block = True
                    code_fence_char = '```'
                    code_lines = []
                    i += 1
                    continue
                else:
                    # End code block
                    in_code_block = False
                    if code_lines:
                        self.elements.append(('code', '\n'.join(code_lines)))
                    code_lines = []
                    code_fence_char = None
                    i += 1
                    continue

            if line.startswith('~~~'):
                if not in_code_block:
                    in_code_block = True
                    code_fence_char = '~~~'
                    code_lines = []
                    i += 1
                    continue
                else:
                    in_code_block = False
                    if code_lines:
                        self.elements.append(('code', '\n'.join(code_lines)))
                    code_lines = []
                    code_fence_char = None
                    i += 1
                    continue

            # Inside code block, collect lines
            if in_code_block:
                code_lines.append(line)
                i += 1
                continue

            # Handle empty lines
            if not line.strip():
                self.elements.append(('empty', ''))
                i += 1
                continue

            # Handle horizontal rules
            if line.strip() in ['---', '***', '___', '----', '*****']:
                self.elements.append(('hr', ''))
                i += 1
                continue

            # Handle headings
            if line.startswith('#'):
                level = 0
                for char in line:
                    if char == '#':
                        level += 1
                    else:
                        break
                if level <= 6 and len(line) > level and line[level] == ' ':
                    text = line[level:].strip()
                    self.elements.append(('heading', text, level))
                    i += 1
                    continue

            # Handle bullet lists
            stripped = line.lstrip()
            if stripped.startswith(('-', '*', '+')) and len(stripped) > 1 and stripped[1] in ' \t':
                text = stripped[1:].strip()
                self.elements.append(('bullet', text))
                i += 1
                continue

            # Handle numbered lists
            if stripped and stripped[0].isdigit():
                match = False
                for j in range(1, min(10, len(stripped))):
                    if stripped[j] == '.' and j + 1 < len(stripped) and stripped[j + 1] in ' \t':
                        text = stripped[j + 1:].strip()
                        self.elements.append(('numbered', text))
                        match = True
                        break
                if match:
                    i += 1
                    continue

            # Regular paragraph
            # Accumulate paragraph lines until we hit an empty line or special marker
            para_lines = [line]
            i += 1
            while i < len(lines):
                next_line = lines[i]
                # Stop at empty line or special markers
                if not next_line.strip():
                    break
                if next_line.startswith('#') or next_line.startswith('```') or next_line.startswith('~~~'):
                    break
                if next_line.strip() in ['---', '***', '___']:
                    break
                next_stripped = next_line.lstrip()
                if next_stripped.startswith(('-', '*', '+')) and len(next_stripped) > 1 and next_stripped[1] in ' \t':
                    break

                para_lines.append(next_line)
                i += 1

            para_text = ' '.join(para_lines)
            self.elements.append(('paragraph', para_text))

        # Handle unclosed code block
        if in_code_block and code_lines:
            self.elements.append(('code', '\n'.join(code_lines)))

        return self.elements


def create_text_element(text, preserve_spaces=True):
    """Create a text element with optional space preservation."""
    t = ET.Element(f'{{{NAMESPACES["w"]}}}t')
    if preserve_spaces:
        t.set(f'{{{NAMESPACES["w"]}}}space', 'preserve')
    t.text = text
    return t


def create_run_element(text, bold=False, italic=False):
    """Create a DOCX run element with text and optional formatting."""
    run = ET.Element(f'{{{NAMESPACES["w"]}}}r')

    # Run properties
    rpr = ET.SubElement(run, f'{{{NAMESPACES["w"]}}}rPr')

    if bold:
        ET.SubElement(rpr, f'{{{NAMESPACES["w"]}}}b')
        ET.SubElement(rpr, f'{{{NAMESPACES["w"]}}}bCs')

    if italic:
        ET.SubElement(rpr, f'{{{NAMESPACES["w"]}}}i')
        ET.SubElement(rpr, f'{{{NAMESPACES["w"]}}}iCs')

    # Text
    run.append(create_text_element(text))
    return run


def create_paragraph_element(text, style='Normal', bold=False, italic=False):
    """Create a DOCX paragraph element with text and style."""
    p = ET.Element(f'{{{NAMESPACES["w"]}}}p')

    # Paragraph properties
    ppr = ET.SubElement(p, f'{{{NAMESPACES["w"]}}}pPr')
    pstyle = ET.SubElement(ppr, f'{{{NAMESPACES["w"]}}}pStyle')
    pstyle.set(f'{{{NAMESPACES["w"]}}}val', style)

    # Spacing for better readability
    spacing = ET.SubElement(ppr, f'{{{NAMESPACES["w"]}}}spacing')
    spacing.set(f'{{{NAMESPACES["w"]}}}after', '120')
    spacing.set(f'{{{NAMESPACES["w"]}}}line', '276')
    spacing.set(f'{{{NAMESPACES["w"]}}}lineRule', 'auto')

    # Add text run
    p.append(create_run_element(text, bold=bold, italic=italic))

    return p


def create_empty_paragraph():
    """Create an empty paragraph for spacing."""
    p = ET.Element(f'{{{NAMESPACES["w"]}}}p')
    ppr = ET.SubElement(p, f'{{{NAMESPACES["w"]}}}pPr')
    spacing = ET.SubElement(ppr, f'{{{NAMESPACES["w"]}}}spacing')
    spacing.set(f'{{{NAMESPACES["w"]}}}after', '120')
    return p


def build_document_xml(elements):
    """Build the main document.xml content from parsed elements."""
    # Create root element
    document = ET.Element(f'{{{NAMESPACES["w"]}}}document')

    # Body
    body = ET.SubElement(document, f'{{{NAMESPACES["w"]}}}body')

    para_count = 0

    for elem in elements:
        if elem[0] == 'empty':
            body.append(create_empty_paragraph())

        elif elem[0] == 'hr':
            # Horizontal rule - create a paragraph with dashes
            body.append(create_paragraph_element('—' * 50))

        elif elem[0] == 'heading':
            text = elem[1]
            level = elem[2]
            style_map = {
                1: 'Heading1',
                2: 'Heading2',
                3: 'Heading3',
                4: 'Heading4',
                5: 'Heading5',
                6: 'Heading6'
            }
            style = style_map.get(level, 'Heading1')
            # Headings are bold
            body.append(create_paragraph_element(text, style=style, bold=True))
            para_count += 1

        elif elem[0] == 'paragraph':
            text = elem[1]
            # Simple bold/italic parsing - strip markdown markers
            # For reliability, we remove the markers rather than implementing complex parsing
            text = text.replace('**', '').replace('__', '')  # Remove bold markers
            text = text.replace('*', '').replace('_', '')    # Remove italic markers
            body.append(create_paragraph_element(text))
            para_count += 1

        elif elem[0] == 'bullet':
            text = '• ' + elem[1]
            body.append(create_paragraph_element(text))
            para_count += 1

        elif elem[0] == 'numbered':
            text = elem[1]
            body.append(create_paragraph_element(text))
            para_count += 1

        elif elem[0] == 'code':
            text = elem[1]
            body.append(create_paragraph_element(text))
            para_count += 1

    # Section properties for proper page setup
    sectPr = ET.SubElement(body, f'{{{NAMESPACES["w"]}}}sectPr')
    pgSz = ET.SubElement(sectPr, f'{{{NAMESPACES["w"]}}}pgSz')
    pgSz.set(f'{{{NAMESPACES["w"]}}}w', '12240')
    pgSz.set(f'{{{NAMESPACES["w"]}}}h', '15840')

    pgMar = ET.SubElement(sectPr, f'{{{NAMESPACES["w"]}}}pgMar')
    pgMar.set(f'{{{NAMESPACES["w"]}}}top', '1440')
    pgMar.set(f'{{{NAMESPACES["w"]}}}right', '1440')
    pgMar.set(f'{{{NAMESPACES["w"]}}}bottom', '1440')
    pgMar.set(f'{{{NAMESPACES["w"]}}}left', '1440')
    pgMar.set(f'{{{NAMESPACES["w"]}}}header', '720')
    pgMar.set(f'{{{NAMESPACES["w"]}}}footer', '720')
    pgMar.set(f'{{{NAMESPACES["w"]}}}gutter', '0')

    return document, para_count


def create_content_types_xml():
    """Create [Content_Types].xml for the DOCX package."""
    root = ET.Element(f'{{{NAMESPACES["cp"]}}}Types')
    root.set('xmlns', f'{NAMESPACES["cp"]}')

    # Default content types
    defaults = [
        ('rels', 'application/vnd.openxmlformats-package.relationships+xml'),
        ('xml', 'application/xml'),
    ]

    for ext, ctype in defaults:
        d = ET.SubElement(root, f'{{{NAMESPACES["cp"]}}}Default')
        d.set('Extension', ext)
        d.set('ContentType', ctype)

    # Overrides for specific parts
    overrides = [
        ('/word/document.xml', 'application/vnd.openxmlformats-wordprocessingml.document.main+xml'),
        ('/word/_rels/document.xml.rels', 'application/vnd.openxmlformats-package.relationships+xml'),
        ('/word/styles.xml', 'application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml'),
        ('/docProps/core.xml', 'application/vnd.openxmlformats-package.core-properties+xml'),
        ('/docProps/app.xml', 'application/vnd.openxmlformats-officedocument.extended-properties+xml'),
    ]

    for path, ctype in overrides:
        o = ET.SubElement(root, f'{{{NAMESPACES["cp"]}}}Override')
        o.set('PartName', path)
        o.set('ContentType', ctype)

    return root


def create_rels_xml():
    """Create _rels/.rels for package relationships."""
    root = ET.Element(f'{{{NAMESPACES["r"]}}}Relationships')
    root.set('xmlns', f'{NAMESPACES["r"]}')

    # Relationship to the main document
    rel = ET.SubElement(root, f'{{{NAMESPACES["r"]}}}Relationship')
    rel.set('Id', 'rId1')
    rel.set('Type', 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument')
    rel.set('Target', 'word/document.xml')

    # Relationship to core properties
    rel2 = ET.SubElement(root, f'{{{NAMESPACES["r"]}}}Relationship')
    rel2.set('Id', 'rId2')
    rel2.set('Type', 'http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties')
    rel2.set('Target', 'docProps/core.xml')

    # Relationship to extended properties
    rel3 = ET.SubElement(root, f'{{{NAMESPACES["r"]}}}Relationship')
    rel3.set('Id', 'rId3')
    rel3.set('Type', 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties')
    rel3.set('Target', 'docProps/app.xml')

    return root


def create_document_rels_xml():
    """Create word/_rels/document.xml.rels for document relationships."""
    root = ET.Element(f'{{{NAMESPACES["r"]}}}Relationships')
    root.set('xmlns', f'{NAMESPACES["r"]}')

    # Relationship to styles
    rel = ET.SubElement(root, f'{{{NAMESPACES["r"]}}}Relationship')
    rel.set('Id', 'rId1')
    rel.set('Type', 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles')
    rel.set('Target', 'styles.xml')

    return root


def create_styles_xml():
    """Create word/styles.xml with heading and paragraph styles."""
    root = ET.Element(f'{{{NAMESPACES["w"]}}}styles')
    root.set('xmlns', f'{NAMESPACES["w"]}')
    root.set('xmlns:r', f'{NAMESPACES["r"]}')

    # Normal style
    style = ET.SubElement(root, f'{{{NAMESPACES["w"]}}}style')
    style.set(f'{{{NAMESPACES["w"]}}}type', 'paragraph')
    style.set(f'{{{NAMESPACES["w"]}}}styleId', 'Normal')

    name = ET.SubElement(style, f'{{{NAMESPACES["w"]}}}name')
    name.set(f'{{{NAMESPACES["w"]}}}val', 'Normal')

    qformat = ET.SubElement(style, f'{{{NAMESPACES["w"]}}}qFormat')

    # Normal paragraph properties
    pPr = ET.SubElement(style, f'{{{NAMESPACES["w"]}}}pPr')
    spacing = ET.SubElement(pPr, f'{{{NAMESPACES["w"]}}}spacing')
    spacing.set(f'{{{NAMESPACES["w"]}}}after', '120')
    spacing.set(f'{{{NAMESPACES["w"]}}}line', '276')
    spacing.set(f'{{{NAMESPACES["w"]}}}lineRule', 'auto')

    # Normal run properties (font)
    rPr = ET.SubElement(style, f'{{{NAMESPACES["w"]}}}rPr')
    rFonts = ET.SubElement(rPr, f'{{{NAMESPACES["w"]}}}rFonts')
    rFonts.set(f'{{{NAMESPACES["w"]}}}ascii', 'Times New Roman')
    rFonts.set(f'{{{NAMESPACES["w"]}}}hAnsi', 'Times New Roman')
    rFonts.set(f'{{{NAMESPACES["w"]}}}cs', 'Times New Roman')

    sz = ET.SubElement(rPr, f'{{{NAMESPACES["w"]}}}sz')
    sz.set(f'{{{NAMESPACES["w"]}}}val', '24')  # 12pt

    szCs = ET.SubElement(rPr, f'{{{NAMESPACES["w"]}}}szCs')
    szCs.set(f'{{{NAMESPACES["w"]}}}val', '24')

    # Heading styles
    heading_sizes = {
        1: '32',   # 16pt
        2: '28',   # 14pt
        3: '26',   # 13pt
        4: '24',   # 12pt
        5: '22',   # 11pt
        6: '20',   # 10pt
    }

    for i in range(1, 7):
        style = ET.SubElement(root, f'{{{NAMESPACES["w"]}}}style')
        style.set(f'{{{NAMESPACES["w"]}}}type', 'paragraph')
        style.set(f'{{{NAMESPACES["w"]}}}styleId', f'Heading{i}')

        name = ET.SubElement(style, f'{{{NAMESPACES["w"]}}}name')
        name.set(f'{{{NAMESPACES["w"]}}}val', f'Heading {i}')

        qformat = ET.SubElement(style, f'{{{NAMESPACES["w"]}}}qFormat')

        # Paragraph properties for heading
        pPr = ET.SubElement(style, f'{{{NAMESPACES["w"]}}}pPr')
        spacing = ET.SubElement(pPr, f'{{{NAMESPACES["w"]}}}spacing')
        spacing.set(f'{{{NAMESPACES["w"]}}}before', '240')
        spacing.set(f'{{{NAMESPACES["w"]}}}after', '120')

        keepNext = ET.SubElement(pPr, f'{{{NAMESPACES["w"]}}}keepNext')
        keepLines = ET.SubElement(pPr, f'{{{NAMESPACES["w"]}}}keepLines')

        # Run properties for heading (bold, font size)
        rPr = ET.SubElement(style, f'{{{NAMESPACES["w"]}}}rPr')

        b = ET.SubElement(rPr, f'{{{NAMESPACES["w"]}}}b')
        bCs = ET.SubElement(rPr, f'{{{NAMESPACES["w"]}}}bCs')

        rFonts = ET.SubElement(rPr, f'{{{NAMESPACES["w"]}}}rFonts')
        rFonts.set(f'{{{NAMESPACES["w"]}}}ascii', 'Calibri')
        rFonts.set(f'{{{NAMESPACES["w"]}}}hAnsi', 'Calibri')
        rFonts.set(f'{{{NAMESPACES["w"]}}}cs', 'Calibri')

        sz = ET.SubElement(rPr, f'{{{NAMESPACES["w"]}}}sz')
        sz.set(f'{{{NAMESPACES["w"]}}}val', heading_sizes[i])

        szCs = ET.SubElement(rPr, f'{{{NAMESPACES["w"]}}}szCs')
        szCs.set(f'{{{NAMESPACES["w"]}}}val', heading_sizes[i])

    return root


def create_core_props_xml():
    """Create docProps/core.xml with document metadata."""
    root = ET.Element(f'{{{NAMESPACES["cp"]}}}coreProperties')
    root.set('xmlns:cp', f'{NAMESPACES["cp"]}')
    root.set('xmlns:dc', f'{NAMESPACES["dc"]}')
    root.set('xmlns:dcterms', f'{NAMESPACES["dcterms"]}')
    root.set('xmlns:dcmitype', f'{NAMESPACES["dcmitype"]}')
    root.set('xmlns:xsi', f'{NAMESPACES["xsi"]}')

    # Title
    title = ET.SubElement(root, f'{{{NAMESPACES["dc"]}}}title')
    title.text = 'Markdown Document'

    # Subject
    subject = ET.SubElement(root, f'{{{NAMESPACES["dc"]}}}subject')
    subject.text = 'Converted from Markdown'

    # Creator
    creator = ET.SubElement(root, f'{{{NAMESPACES["dc"]}}}creator')
    creator.text = 'md-to-docx Converter'

    # Keywords
    keywords = ET.SubElement(root, f'{{{NAMESPACES["cp"]}}}keywords')
    keywords.text = 'markdown,document,conversion'

    # Description
    description = ET.SubElement(root, f'{{{NAMESPACES["dc"]}}}description')
    description.text = 'Document converted from Markdown format'

    # Last modified by
    lastModifiedBy = ET.SubElement(root, f'{{{NAMESPACES["cp"]}}}lastModifiedBy')
    lastModifiedBy.text = 'md-to-docx Converter'

    # Created date
    created = ET.SubElement(root, f'{{{NAMESPACES["dcterms"]}}}created')
    created.set(f'{{{NAMESPACES["xsi"]}}}type', 'dcterms:W3CDTF')
    created.text = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    # Modified date
    modified = ET.SubElement(root, f'{{{NAMESPACES["dcterms"]}}}modified')
    modified.set(f'{{{NAMESPACES["xsi"]}}}type', 'dcterms:W3CDTF')
    modified.text = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    return root


def create_app_props_xml():
    """Create docProps/app.xml with application properties."""
    root = ET.Element(f'{{{NAMESPACES["ep"]}}}Properties')
    root.set('xmlns:ep', f'{NAMESPACES["ep"]}')
    root.set('xmlns:vt', f'{NAMESPACES["vt"]}')

    # Application
    app = ET.SubElement(root, f'{{{NAMESPACES["ep"]}}}Application')
    app.text = 'md-to-docx Converter'

    # Version
    version = ET.SubElement(root, f'{{{NAMESPACES["ep"]}}}AppVersion')
    version.text = '1.0.0'

    # Total time
    totalTime = ET.SubElement(root, f'{{{NAMESPACES["ep"]}}}TotalTime')
    totalTime.text = '0'

    # Pages
    pages = ET.SubElement(root, f'{{{NAMESPACES["ep"]}}}Pages')
    pages.text = '1'

    # Words
    words = ET.SubElement(root, f'{{{NAMESPACES["ep"]}}}Words')
    words.text = '0'

    # Characters
    characters = ET.SubElement(root, f'{{{NAMESPACES["ep"]}}}Characters')
    characters.text = '0'

    # Characters with spaces
    charactersWithSpaces = ET.SubElement(root, f'{{{NAMESPACES["ep"]}}}CharactersWithSpaces')
    charactersWithSpaces.text = '0'

    # Lines
    lines = ET.SubElement(root, f'{{{NAMESPACES["ep"]}}}Lines')
    lines.text = '0'

    # Paragraphs
    paragraphs = ET.SubElement(root, f'{{{NAMESPACES["ep"]}}}Paragraphs')
    paragraphs.text = '1'

    return root


def write_docx(output_path, document_xml):
    """Write the DOCX file as a ZIP archive with proper structure."""
    # Create output directory if needed
    resolved_output_path = workspace_path(output_path)
    output_dir = os.path.dirname(resolved_output_path)
    if output_dir and not os.path.exists(output_dir):
        try:
            os.makedirs(output_dir, exist_ok=True)
        except Exception as e:
            raise ValueError(f"Cannot create output directory: {e}")

    try:
        with zipfile.ZipFile(resolved_output_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            # [Content_Types].xml
            content_types = create_content_types_xml()
            zf.writestr('[Content_Types].xml', ET.tostring(content_types, encoding='utf-8', xml_declaration=True))

            # _rels/.rels
            rels = create_rels_xml()
            zf.writestr('_rels/.rels', ET.tostring(rels, encoding='utf-8', xml_declaration=True))

            # word/document.xml
            zf.writestr('word/document.xml', ET.tostring(document_xml, encoding='utf-8', xml_declaration=True))

            # word/_rels/document.xml.rels
            doc_rels = create_document_rels_xml()
            zf.writestr('word/_rels/document.xml.rels', ET.tostring(doc_rels, encoding='utf-8', xml_declaration=True))

            # word/styles.xml
            styles = create_styles_xml()
            zf.writestr('word/styles.xml', ET.tostring(styles, encoding='utf-8', xml_declaration=True))

            # docProps/core.xml
            core_props = create_core_props_xml()
            zf.writestr('docProps/core.xml', ET.tostring(core_props, encoding='utf-8', xml_declaration=True))

            # docProps/app.xml
            app_props = create_app_props_xml()
            zf.writestr('docProps/app.xml', ET.tostring(app_props, encoding='utf-8', xml_declaration=True))

    except Exception as e:
        raise ValueError(f"Error writing DOCX file: {e}")


def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description='Convert Markdown files to DOCX format',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s README.md output.docx
  %(prog)s docs/review.md report.docx
        """
    )

    parser.add_argument('input_md', help='Input Markdown file (workspace-relative path)')
    parser.add_argument('output_docx', help='Output DOCX file (workspace-relative path)')

    args = parser.parse_args()

    try:
        # Validate paths
        input_path = validate_path(args.input_md, "Input path")
        output_path = validate_path(args.output_docx, "Output path")

        # Validate extensions
        validate_extensions(input_path, output_path)

        # Read Markdown
        md_content = read_markdown(input_path)

        # Parse Markdown
        parser_obj = MarkdownParser(md_content)
        elements = parser_obj.parse()

        # Build document
        document_xml, para_count = build_document_xml(elements)

        # Write DOCX
        write_docx(output_path, document_xml)

        # Success message
        print(f"✓ Converted '{input_path}' to '{output_path}' (~{para_count} paragraphs)")

    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
